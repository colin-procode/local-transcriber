#!/usr/bin/env python3
"""Local meeting and media transcriber UI."""

from __future__ import annotations

import json
import math
import os
import re
import signal
import socket
import shutil
import subprocess
import threading
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "static"
TRANSCRIPTS_DIR = ROOT / "transcripts"


def default_bind_host() -> str:
    """Use WSL's private VM address so Windows browsers can reach the app."""
    try:
        kernel = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except OSError:
        kernel = ""
    if "microsoft" not in kernel and "wsl" not in os.environ.get("WSL_DISTRO_NAME", "").lower():
        return "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route_probe:
            route_probe.connect(("192.0.2.1", 80))
            address = route_probe.getsockname()[0]
        return address if not address.startswith("127.") else "127.0.0.1"
    except OSError:
        return "127.0.0.1"


APP_HOST = os.environ.get("TRANSCRIBER_HOST", default_bind_host())
APP_PORT = int(os.environ.get("TRANSCRIBER_PORT", "8000"))
MODEL_HOST = "127.0.0.1"
MODEL_PORT = int(os.environ.get("TRANSCRIBER_MODEL_PORT", "8766"))
MODEL_URL = f"http://{MODEL_HOST}:{MODEL_PORT}"
QWEN_HOST = os.environ.get("TRANSCRIBER_QWEN_HOST", APP_HOST)
QWEN_PORT = int(os.environ.get("TRANSCRIBER_QWEN_PORT", "8767"))
QWEN_HEALTH_PORT = QWEN_PORT + 1
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
QWEN_PYTHON = DATA_HOME / "transcriber" / "qwen-venv" / "bin" / "python"

_process: subprocess.Popen[bytes] | None = None
_process_lock = threading.Lock()
_process_started_by_app = False
_qwen_process: subprocess.Popen[bytes] | None = None
_qwen_process_started_by_app = False
_log_path = TRANSCRIPTS_DIR / ".model-runtime.log"
_qwen_log_path = TRANSCRIPTS_DIR / ".qwen-runtime.log"
_app_error_log_path = TRANSCRIPTS_DIR / ".app-errors.log"
_file_progress_lock = threading.Lock()
_file_progress = {"active": False, "stage": "idle", "percent": 0, "detail": ""}


def set_file_progress(stage: str, percent: int = 0, detail: str = "") -> None:
    with _file_progress_lock:
        _file_progress.update({"active": stage not in {"idle", "complete", "error"}, "stage": stage, "percent": max(0, min(100, percent)), "detail": detail})


def runtime_binary() -> str | None:
    configured = os.environ.get("NEMO_SPEECH_BIN")
    if configured:
        return configured if Path(configured).exists() else shutil.which(configured)
    return shutil.which("nemo-speech") or (
        str(Path.home() / ".local" / "bin" / "nemo-speech")
        if (Path.home() / ".local" / "bin" / "nemo-speech").is_file()
        else None
    )


def runtime_status() -> dict:
    if runtime_binary() is None:
        return {"ready": False, "runtime": "missing", "message": "Run ./setup.sh to install the local models and runtime, then restart this app."}
    try:
        with urllib.request.urlopen(f"http://{QWEN_HOST}:{QWEN_HEALTH_PORT}/ready", timeout=1.5) as response:
            qwen_status = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        with _process_lock:
            qwen_proc = _qwen_process
        if qwen_proc is not None and qwen_proc.poll() is not None:
            return {"ready": False, "runtime": "error", "message": f"Qwen ASR stopped (exit {qwen_proc.returncode}). See {_qwen_log_path.name}."}
        if not QWEN_PYTHON.is_file():
            return {"ready": False, "runtime": "missing", "message": "Run ./setup.sh to install the local models and runtime, then restart this app."}
        return {"ready": False, "runtime": "loading", "message": "Loading Qwen3-ASR 1.7B and Nemotron diarization…"}

    try:
        with urllib.request.urlopen(f"{MODEL_URL}/ready", timeout=1.5) as response:
            diar_status = json.loads(response.read().decode("utf-8"))
        if not diar_status.get("ready", True):
            return {"ready": False, "runtime": "loading", "message": "Loading Nemotron diarization…"}
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return {"ready": False, "runtime": "loading", "message": "Loading Nemotron diarization…"}
    qwen_status.update({"ready": True, "runtime": "ready", "asr": "Qwen3-ASR 1.7B", "diarization": "Nemotron 3 Diarization"})
    return qwen_status


def start_model_runtime() -> None:
    """Start the local inference server and let it load/download the models."""
    global _process, _process_started_by_app, _qwen_process, _qwen_process_started_by_app
    if runtime_binary() is None:
        return
    if not QWEN_PYTHON.is_file():
        return
    if runtime_status().get("ready"):
        return

    with _process_lock:
        if _process is not None and _process.poll() is None:
            # Keep diarization running if Qwen fails initialization, and retry
            # only the missing ASR process on the next readiness poll.
            if _qwen_process is None or _qwen_process.poll() is not None:
                qwen_log = _qwen_log_path.open("ab", buffering=0)
                try:
                    _qwen_process = subprocess.Popen(
                        [str(QWEN_PYTHON), str(ROOT / "qwen_stream_server.py"), "--host", QWEN_HOST, "--port", str(QWEN_PORT), "--health-port", str(QWEN_HEALTH_PORT)],
                        cwd=ROOT,
                        stdin=subprocess.DEVNULL,
                        stdout=qwen_log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    _qwen_process_started_by_app = True
                except OSError as exc:
                    qwen_log.write(f"Could not start Qwen ASR: {exc}\n".encode())
                    qwen_log.close()
            return
        binary = runtime_binary()
        if binary is None:
            return

        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        log = _log_path.open("ab", buffering=0)
        origin = f"http://{APP_HOST}:{APP_PORT}"
        command = [
            binary,
            "serve",
            "--diar-model",
            "nvidia/Nemotron-3-Diarization",
            "--host",
            MODEL_HOST,
            "--port",
            str(MODEL_PORT),
            "--cors-origin",
            origin,
            "--no-ui",
        ]
        # Qwen needs most of the available 8 GiB GPU memory. Running the
        # separate Nemotron diarizer on CPU avoids both models competing
        # for VRAM at the same time.
        command[2:2] = ["--device", "cpu"]
        try:
            _process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _process_started_by_app = True
        except OSError as exc:
            log.write(f"Could not start NeMo-Speech: {exc}\n".encode())
            log.close()
            return

        qwen_log = _qwen_log_path.open("ab", buffering=0)
        try:
            _qwen_process = subprocess.Popen(
                [str(QWEN_PYTHON), str(ROOT / "qwen_stream_server.py"), "--host", QWEN_HOST, "--port", str(QWEN_PORT), "--health-port", str(QWEN_HEALTH_PORT)],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=qwen_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _qwen_process_started_by_app = True
        except OSError as exc:
            qwen_log.write(f"Could not start Qwen ASR: {exc}\n".encode())
            qwen_log.close()


def stop_process_group(process: subprocess.Popen[bytes] | None) -> None:
    """Stop a model process and any worker processes it spawned."""
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    for _ in range(20):
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def handle_terminate(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def safe_id(value: str) -> str:
    if not re.fullmatch(r"\d{8}-\d{6}_to_\d{8}-\d{6}-\d{3}", value or ""):
        raise ValueError("Invalid transcript id")
    return value


def transcript_path(transcript_id: str) -> Path:
    return TRANSCRIPTS_DIR / f"{safe_id(transcript_id)}.json"


def pretty_time(value: str | None) -> str:
    if not value:
        return "Unknown"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return value


def format_offset(value: object) -> str:
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        seconds = 0.0
    if not math.isfinite(seconds):
        seconds = 0.0
    whole_seconds = int(seconds)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}"


def transcript_markdown(data: dict) -> str:
    title = data.get("title") or "Untitled transcript"
    lines = [f"# {title}", "", f"- Started: {pretty_time(data.get('start_time'))}", f"- Stopped: {pretty_time(data.get('end_time'))}"]
    if data.get("note"):
        lines.extend(["", "## Note", "", data["note"]])
    lines.extend(["", "## Transcript", ""])

    utterances = data.get("utterances", [])
    if utterances:
        for item in sorted(utterances, key=lambda row: (float(row.get("start", 0)), row.get("source", ""))):
            speaker = item.get("speaker", "unknown")
            source = item.get("source", "system")
            name = data.get("speaker_names", {}).get(speaker, speaker)
            timestamp = f"[{format_offset(item.get('start', 0))}–{format_offset(item.get('end', item.get('start', 0)))}]"
            source_label = f" ({source})" if source and source != "system" else ""
            lines.append(f"{timestamp} **{name}**{source_label}: {item.get('text', '')}")
            lines.append("")
    elif data.get("words"):
        words = sorted(data.get("words", []), key=lambda item: (float(item.get("start", 0)), item.get("source", "")))
        grouped: list[dict] = []
        for item in words:
            key = (item.get("speaker", "unknown"), item.get("source", "system"))
            if not grouped or (grouped[-1]["speaker"], grouped[-1]["source"]) != key:
                grouped.append({"speaker": key[0], "source": key[1], "words": [], "start": item.get("start", 0), "end": item.get("end", item.get("start", 0))})
            grouped[-1]["words"].append(str(item.get("word", "")))
            grouped[-1]["end"] = item.get("end", item.get("start", grouped[-1]["end"]))
        for group in grouped:
            speaker, source = group["speaker"], group["source"]
            name = data.get("speaker_names", {}).get(speaker, speaker)
            timestamp = f"[{format_offset(group['start'])}–{format_offset(group['end'])}]"
            source_label = f" ({source})" if source and source != "system" else ""
            lines.append(f"{timestamp} **{name}**{source_label}: {' '.join(group['words'])}")
            lines.append("")
    else:
        lines.append("_(No speech was recognized.)_")
    return "\n".join(lines).rstrip() + "\n"


def load_transcripts() -> list[dict]:
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in TRANSCRIPTS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rows.append({
                "id": data.get("id", path.stem),
                "title": data.get("title") or "Untitled transcript",
                "start_time": data.get("start_time"),
                "end_time": data.get("end_time"),
                "note": data.get("note", ""),
                "word_count": len(data.get("words", [])),
            })
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(rows, key=lambda row: row.get("start_time") or "", reverse=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalTranscriber/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep the console quiet; errors are returned to the UI.
        return

    def send_json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 20_000_000:
            raise ValueError("Request is too large")
        raw = self.rfile.read(size)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/file-progress":
            with _file_progress_lock:
                progress = dict(_file_progress)
            if progress.get("stage") == "transcribing":
                try:
                    with urllib.request.urlopen(f"http://{QWEN_HOST}:{QWEN_HEALTH_PORT}/progress", timeout=1) as response:
                        model_progress = json.loads(response.read().decode("utf-8"))
                    total = int(model_progress.get("total_chunks", 0))
                    done = int(model_progress.get("completed_chunks", 0))
                    if model_progress.get("active") and total:
                        progress.update({
                            "percent": 30 + round(65 * done / total),
                            "detail": f"Processed {done} of {total} audio chunks",
                        })
                    else:
                        progress.update({"percent": max(20, progress["percent"]), "detail": "Transcribing audio chunks…"})
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    pass
            self.send_json(progress)
            return
        if parsed.path == "/api/status":
            status = runtime_status()
            self.send_json({"model": status, "transcripts_dir": str(TRANSCRIPTS_DIR)})
            return
        if parsed.path == "/api/transcripts":
            self.send_json(load_transcripts())
            return
        if parsed.path.startswith("/api/transcripts/") and parsed.path.endswith("/download"):
            transcript_id = unquote(parsed.path.removeprefix("/api/transcripts/").removesuffix("/download").strip("/"))
            try:
                data = json.loads(transcript_path(transcript_id).read_text(encoding="utf-8"))
            except FileNotFoundError:
                self.send_json({"error": "Transcript not found"}, 404)
                return
            except (ValueError, json.JSONDecodeError):
                self.send_json({"error": "Invalid transcript"}, 400)
                return
            content = transcript_markdown(data).encode("utf-8")
            filename = re.sub(r"[^A-Za-z0-9._-]+", "_", str(data.get("title") or "transcript")).strip("._") or "transcript"
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename[:120]}.md"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path.startswith("/api/transcripts/"):
            transcript_id = unquote(parsed.path.rsplit("/", 1)[-1])
            try:
                data = json.loads(transcript_path(transcript_id).read_text(encoding="utf-8"))
            except FileNotFoundError:
                self.send_json({"error": "Transcript not found"}, 404)
            except (ValueError, json.JSONDecodeError):
                self.send_json({"error": "Invalid transcript"}, 400)
            else:
                self.send_json(data)
            return

        request_path = "/index.html" if parsed.path in ("/", "") else parsed.path
        file_path = (WEB_DIR / request_path.lstrip("/")).resolve()
        if WEB_DIR.resolve() not in file_path.parents or not file_path.is_file():
            self.send_error(404)
            return
        content_type = "text/html; charset=utf-8" if file_path.suffix == ".html" else "text/plain; charset=utf-8"
        if file_path.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif file_path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/diarize-live":
            self.diarize_live_audio(parse_qs(parsed.query))
            return
        if parsed.path == "/api/transcribe-file":
            self.transcribe_uploaded_file(parse_qs(parsed.query))
            return
        if parsed.path != "/api/transcripts":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            data = self.read_json()
            start_time = str(data["start_time"])
            end_time = str(data["end_time"])
            start = datetime.fromisoformat(start_time)
            end = datetime.fromisoformat(end_time)
            transcript_id = f"{start.strftime('%Y%m%d-%H%M%S')}_to_{end.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"
            data.update({
                "id": transcript_id,
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "title": str(data.get("title") or "Untitled transcript").strip()[:200],
                "note": str(data.get("note") or "")[:20_000],
                "speaker_names": data.get("speaker_names") or {},
                "words": data.get("words") or [],
                "utterances": data.get("utterances") or [],
            })
            TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
            path = transcript_path(transcript_id)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            path.with_suffix(".md").write_text(transcript_markdown(data), encoding="utf-8")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json({"error": f"Could not save transcript: {exc}"}, 400)
            return
        except OSError as exc:
            self.send_json({"error": f"Could not write transcript: {exc}"}, 500)
            return
        self.send_json({"id": transcript_id, "transcript": data}, 201)

    def diarize_live_audio(self, query: dict[str, list[str]]) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 44 or content_length > 5_000_000_000:
            self.send_json({"error": "The live audio payload is empty or too large."}, 413)
            return
        received = 0
        set_file_progress("diarizing", 0, "Adding speaker labels to live audio…")
        try:
            with tempfile.TemporaryDirectory(prefix="local-transcriber-live-") as temporary:
                wav_path = Path(temporary) / "live.wav"
                with wav_path.open("wb") as destination:
                    while received < content_length:
                        chunk = self.rfile.read(min(1024 * 1024, content_length - received))
                        if not chunk:
                            raise ValueError("Live audio upload ended before the complete recording arrived")
                        destination.write(chunk)
                        received += len(chunk)
                with wave.open(str(wav_path), "rb") as audio:
                    if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
                        raise ValueError("Live audio must be 16 kHz mono PCM")
                result = self.diarize_file(wav_path)
            set_file_progress("complete", 100, "Speaker detection complete")
            self.send_json({"segments": result.get("segments", []), "source": query.get("source", ["system"])[0]})
        except Exception as exc:
            set_file_progress("error", 0, str(exc) or "Speaker detection failed")
            try:
                with _app_error_log_path.open("a", encoding="utf-8") as log:
                    log.write(f"\n--- {datetime.now().astimezone().isoformat()} live diarization ---\n")
                    log.write(traceback.format_exc())
            except OSError:
                pass
            if received < content_length:
                self.close_connection = True
                return
            try:
                self.send_json({"error": f"Live diarization failed ({type(exc).__name__}): {exc}"}, 500)
            except OSError:
                self.close_connection = True

    def transcribe_uploaded_file(self, query: dict[str, list[str]]) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > 5_000_000_000:
            self.send_json({"error": "The media file is empty or larger than the 5 GB upload limit."}, 413)
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.send_json({"error": "FFmpeg is required for local file decoding. Install FFmpeg and restart the app."}, 503)
            return

        filename = Path(query.get("filename", ["media"])[0]).name or "media"
        transcript_source = "mic" if query.get("source", ["system"])[0] == "mic" else "system"
        speaker_prefix = "MIC_SPEAKER" if transcript_source == "mic" else "COMPUTER_SPEAKER"
        received = 0
        set_file_progress("uploading", 0, "Uploading file to the local app…")
        try:
            with tempfile.TemporaryDirectory(prefix="local-transcriber-") as temporary:
                source_path = Path(temporary) / filename
                wav_path = Path(temporary) / "audio.wav"
                with source_path.open("wb") as destination:
                    while received < content_length:
                        chunk = self.rfile.read(min(1024 * 1024, content_length - received))
                        if not chunk:
                            raise ValueError("Upload ended before the complete file arrived")
                        destination.write(chunk)
                        received += len(chunk)
                        set_file_progress("uploading", round(20 * received / content_length), f"Uploaded {received // (1024 * 1024)} of {max(1, content_length // (1024 * 1024))} MB")

                set_file_progress("decoding", 20, "Extracting audio from the file…")
                decoded = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=1800,
                )
                if decoded.returncode:
                    detail = decoded.stderr.decode("utf-8", errors="replace").strip().splitlines()
                    raise ValueError(detail[-1] if detail else "FFmpeg could not decode this media file")
                if not wav_path.is_file() or wav_path.stat().st_size <= 44:
                    raise ValueError("No audio track was found in this media file")

                warning = ""
                set_file_progress("diarizing", 25, "Detecting speakers…")
                try:
                    diarization = self.diarize_file(wav_path)
                    diar_segments = diarization.get("segments", [])
                except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
                    diar_segments = []
                    warning = str(exc)

                qwen_body = json.dumps({"path": str(wav_path), "segments": diar_segments}).encode("utf-8")
                set_file_progress("transcribing", 30, "Preparing audio chunks…")
                qwen_request = urllib.request.Request(
                    f"http://{QWEN_HOST}:{QWEN_HEALTH_PORT}/transcribe-file",
                    data=qwen_body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(qwen_request, timeout=7200) as response:
                    qwen_result = json.loads(response.read().decode("utf-8"))

                utterances = []
                speaker_names = {}
                for segment in qwen_result.get("segments", []):
                    start = float(segment.get("start", 0))
                    end = float(segment.get("end", start))
                    best_speaker = None
                    best_overlap = 0.0
                    for diar in diar_segments:
                        diar_start = float(diar.get("start", diar.get("start_time", 0)))
                        diar_end = float(diar.get("end", diar.get("end_time", diar_start)))
                        overlap = max(0.0, min(end, diar_end) - max(start, diar_start))
                        if overlap > best_overlap:
                            best_speaker = diar.get("speaker", diar.get("speaker_id"))
                            best_overlap = overlap
                    match = re.search(r"(\d+)$", str(best_speaker)) if best_speaker is not None else None
                    speaker = f"{speaker_prefix}_{match.group(1) if match else '1'}"
                    speaker_names.setdefault(speaker, speaker.replace("_", " ").title())
                    utterances.append({"source": transcript_source, "speaker": speaker, "text": str(segment.get("text", "")).strip(), "start": start, "end": end})

                set_file_progress("complete", 100, "Transcription complete")
                self.send_json({
                    "duration": qwen_result.get("duration", 0),
                    "utterances": utterances,
                    "speaker_names": speaker_names,
                    "warning": warning,
                })
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            set_file_progress("error", 0, str(exc) or "Transcription failed")
            if received < content_length:
                self.close_connection = True
            self.send_json({"error": str(exc) or "Could not transcribe this media file."}, 500)
        except Exception as exc:
            set_file_progress("error", 0, str(exc) or "Transcription failed")
            try:
                with _app_error_log_path.open("a", encoding="utf-8") as log:
                    log.write(f"\n--- {datetime.now().astimezone().isoformat()} file transcription ---\n")
                    log.write(traceback.format_exc())
            except OSError:
                pass
            if received < content_length:
                self.close_connection = True
                return
            try:
                self.send_json({"error": f"Transcription failed ({type(exc).__name__}): {exc}"}, 500)
            except OSError:
                self.close_connection = True

    def diarize_file(self, wav_path: Path) -> dict:
        import http.client
        import uuid

        boundary = "----LocalTranscriber" + uuid.uuid4().hex
        prefix = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
            "Content-Type: audio/wav\r\n\r\n"
        ).encode("ascii")
        middle = (
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\nstreaming\r\n"
            f"--{boundary}--\r\n"
        ).encode("ascii")
        length = len(prefix) + wav_path.stat().st_size + len(middle)
        connection = http.client.HTTPConnection(MODEL_HOST, MODEL_PORT, timeout=7200)
        try:
            connection.putrequest("POST", "/v1/audio/diarizations")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(length))
            connection.endheaders()
            connection.send(prefix)
            with wav_path.open("rb") as audio:
                while chunk := audio.read(1024 * 1024):
                    connection.send(chunk)
            connection.send(middle)
            response = connection.getresponse()
            payload = response.read()
            if response.status < 200 or response.status >= 300:
                raise ValueError(f"Nemotron diarization returned HTTP {response.status}: {payload[:500].decode('utf-8', errors='replace')}")
            return json.loads(payload.decode("utf-8"))
        finally:
            connection.close()

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/transcripts/"):
            self.send_json({"error": "Not found"}, 404)
            return
        transcript_id = unquote(parsed.path.rsplit("/", 1)[-1])
        try:
            path = transcript_path(transcript_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            changes = self.read_json()
            if "title" in changes:
                data["title"] = str(changes["title"]).strip()[:200] or "Untitled transcript"
            if "note" in changes:
                data["note"] = str(changes["note"])[:20_000]
            if "speaker_names" in changes and isinstance(changes["speaker_names"], dict):
                data["speaker_names"] = {
                    str(key): str(value).strip()[:100]
                    for key, value in changes["speaker_names"].items()
                }
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            path.with_suffix(".md").write_text(transcript_markdown(data), encoding="utf-8")
        except FileNotFoundError:
            self.send_json({"error": "Transcript not found"}, 404)
            return
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json({"error": f"Could not update transcript: {exc}"}, 400)
            return
        except OSError as exc:
            self.send_json({"error": f"Could not update transcript: {exc}"}, 500)
            return
        self.send_json(data)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/transcripts/"):
            self.send_json({"error": "Not found"}, 404)
            return
        transcript_id = unquote(parsed.path.rsplit("/", 1)[-1])
        try:
            path = transcript_path(transcript_id)
            path.unlink()
            path.with_suffix(".md").unlink(missing_ok=True)
        except FileNotFoundError:
            self.send_json({"error": "Transcript not found"}, 404)
            return
        except (ValueError, OSError) as exc:
            self.send_json({"error": f"Could not delete transcript: {exc}"}, 500)
            return
        self.send_json({"deleted": transcript_id})


def main() -> None:
    signal.signal(signal.SIGTERM, handle_terminate)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    start_model_runtime()
    server = ThreadingHTTPServer((APP_HOST, APP_PORT), Handler)
    loopback_server = None
    if APP_HOST not in {"127.0.0.1", "localhost"}:
        try:
            loopback_server = ThreadingHTTPServer(("127.0.0.1", APP_PORT), Handler)
            threading.Thread(target=loopback_server.serve_forever, daemon=True).start()
        except OSError as exc:
            print(f"Loopback listener unavailable: {exc}", flush=True)
    print(f"Transcriber UI: http://{APP_HOST}:{APP_PORT}")
    if loopback_server:
        print(f"WSL loopback URL: http://127.0.0.1:{APP_PORT}")
    print(f"Transcript files: {TRANSCRIPTS_DIR}")
    print("Press Ctrl+C to stop the UI. The model server is stopped with this app if this app started it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if loopback_server:
            loopback_server.shutdown()
            loopback_server.server_close()
        if _qwen_process_started_by_app:
            with _process_lock:
                stop_process_group(_qwen_process)
        if _process_started_by_app:
            with _process_lock:
                stop_process_group(_process)


if __name__ == "__main__":
    main()
