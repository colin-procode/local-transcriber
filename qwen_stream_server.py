#!/usr/bin/env python3
"""Local WebSocket and batch adapter for Qwen3-ASR."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import websockets
from qwen_asr import Qwen3ASRModel


ready = False
asr: Qwen3ASRModel | None = None
backend_mode = "vllm"
asr_lock = threading.Lock()
progress_lock = threading.Lock()
file_progress = {"active": False, "stage": "idle", "completed_chunks": 0, "total_chunks": 0, "duration": 0.0}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/progress":
            with progress_lock:
                payload = json.dumps(file_progress).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps({"ready": ready, "model": "Qwen3-ASR 1.7B", "backend": backend_mode}).encode()
        self.send_response(200 if ready else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/transcribe-file":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 1_000_000:
                raise ValueError("Invalid batch transcription request")
            request = json.loads(self.rfile.read(size).decode("utf-8"))
            path = os.path.realpath(str(request.get("path", "")))
            if not path or not os.path.isfile(path):
                raise ValueError("Decoded audio file is missing")
            result = transcribe_file(path, request.get("segments", []))
            payload = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (ValueError, OSError, wave.Error, json.JSONDecodeError) as exc:
            with progress_lock:
                file_progress.update({"active": False, "stage": "Error", "detail": str(exc)})
            payload = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        return


async def send_event(websocket, event: dict) -> None:
    await websocket.send(json.dumps(event, separators=(",", ":")))


async def handle_audio(websocket) -> None:
    try:
        await process_audio(websocket)
    finally:
        print(
            f"audio websocket closed code={websocket.close_code} reason={websocket.close_reason!r}",
            flush=True,
        )


async def process_audio(websocket) -> None:
    assert asr is not None
    state = asr.init_streaming_state(language="English", chunk_size_sec=2.0)
    sample_count = 0
    last_text = ""
    async for message in websocket:
        try:
            if isinstance(message, bytes):
                pcm = np.frombuffer(message, dtype="<i2")
                if not pcm.size:
                    continue
                audio = pcm.astype(np.float32) / 32768.0
                sample_count += int(pcm.size)
                await asyncio.to_thread(streaming_step, audio, state)
                if state.text and state.text != last_text:
                    last_text = state.text
                    await send_event(websocket, {
                        "type": "conversation.item.input_audio_transcription.delta",
                        "delta": state.text,
                    })
                continue

            control = json.loads(message)
            if control.get("type") not in {"input_audio_buffer.commit", "commit"}:
                continue
            await asyncio.to_thread(finish_step, state)
            transcript = str(state.text or "").strip()
            await send_event(websocket, {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": transcript,
                "start": float(control.get("start", 0.0)),
                "end": float(control.get("end", sample_count / 16000.0)),
                "words": [],
            })
            sample_count = 0
            last_text = ""
            state = asr.init_streaming_state(language="English", chunk_size_sec=2.0)
        except Exception as exc:  # Return inference errors to the UI without dropping the stream.
            await send_event(websocket, {"type": "error", "error": {"message": str(exc)}})


async def run_websocket_server(host: str, port: int) -> None:
    hosts = list(dict.fromkeys((host, "127.0.0.1")))
    async with websockets.serve(handle_audio, hosts, port, max_size=None, ping_interval=20, ping_timeout=20):
        await asyncio.Future()


def streaming_step(audio: np.ndarray, state) -> None:
    assert asr is not None
    with asr_lock:
        asr.streaming_transcribe(audio, state)


def finish_step(state) -> None:
    assert asr is not None
    with asr_lock:
        asr.finish_streaming_transcribe(state)


def transcribe_file(path: str, diarization_segments: list[dict] | None = None) -> dict:
    """Run local batch inference over diarized spans without repeated streaming decodes."""
    with wave.open(path, "rb") as audio_file:
        if audio_file.getnchannels() != 1 or audio_file.getsampwidth() != 2 or audio_file.getframerate() != 16000:
            raise ValueError("Audio decoder did not produce 16 kHz mono PCM")
        total_samples = audio_file.getnframes()
        audio = np.frombuffer(audio_file.readframes(total_samples), dtype="<i2").astype(np.float32) / 32768.0

    duration = total_samples / 16000.0
    with progress_lock:
        file_progress.update({"active": True, "stage": "Preparing audio chunks", "completed_chunks": 0, "total_chunks": 0, "duration": duration})
    spans: list[tuple[float, float]] = []
    for segment in diarization_segments or []:
        try:
            start = max(0.0, min(duration, float(segment.get("start", segment.get("start_time", 0)))))
            end = max(start, min(duration, float(segment.get("end", segment.get("end_time", start)))))
        except (AttributeError, TypeError, ValueError):
            continue
        if end > start:
            spans.append((start, end))
    if not spans:
        spans = [(0.0, duration)] if duration else []

    work: list[tuple[float, float, np.ndarray]] = []
    max_span_seconds = 25.0
    for start, end in spans:
        cursor = start
        while cursor < end:
            stop = min(end, cursor + max_span_seconds)
            first = int(cursor * 16000)
            last = int(stop * 16000)
            work.append((cursor, stop, audio[first:last]))
            cursor = stop

    results: list[dict] = []
    batch_size = 1 if backend_mode == "transformers" else 8
    with progress_lock:
        file_progress.update({"stage": "Transcribing audio", "total_chunks": len(work)})
    for offset in range(0, len(work), batch_size):
        batch = work[offset:offset + batch_size]
        with asr_lock:
            transcriptions = asr.transcribe(
                audio=[(samples, 16000) for _, _, samples in batch],
                language="English",
            )
        for (start, end, _samples), transcription in zip(batch, transcriptions):
            text = str(transcription.text or "").strip()
            if text:
                results.append({"text": text, "start": start, "end": end})
        with progress_lock:
            file_progress["completed_chunks"] = min(offset + len(batch), len(work))
    with progress_lock:
        file_progress.update({"active": False, "stage": "Complete", "completed_chunks": len(work), "total_chunks": len(work)})
    return {"duration": duration, "segments": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--health-port", type=int, default=8768)
    args = parser.parse_args()

    global asr, backend_mode, ready
    health_servers = [ThreadingHTTPServer((args.host, args.health_port), HealthHandler)]
    if args.host not in {"127.0.0.1", "localhost"}:
        try:
            health_servers.append(ThreadingHTTPServer(("127.0.0.1", args.health_port), HealthHandler))
        except OSError as exc:
            print(f"Loopback health listener unavailable: {exc}", flush=True)
    for health in health_servers:
        threading.Thread(target=health.serve_forever, daemon=True).start()
    backend_mode = os.environ.get("TRANSCRIBER_QWEN_BACKEND", "auto").strip().lower()
    if backend_mode not in {"auto", "vllm", "transformers"}:
        parser.error("TRANSCRIBER_QWEN_BACKEND must be auto, vllm, or transformers")
    if backend_mode == "auto":
        import torch

        backend_mode = "transformers" if torch.version.hip else "vllm"

    if backend_mode == "transformers":
        import torch

        asr = Qwen3ASRModel.from_pretrained(
            "Qwen/Qwen3-ASR-1.7B",
            dtype=torch.float16,
            device_map="cuda:0",
            max_inference_batch_size=1,
            max_new_tokens=64,
        )
    else:
        asr = Qwen3ASRModel.LLM(
            model="Qwen/Qwen3-ASR-1.7B",
            gpu_memory_utilization=0.85,
            max_model_len=8192,
            max_num_seqs=1,
            max_num_batched_tokens=512,
            enforce_eager=True,
            max_new_tokens=64,
        )
    ready = True
    print(f"Qwen ASR ready with {backend_mode} backend", flush=True)
    asyncio.run(run_websocket_server(args.host, args.port))


if __name__ == "__main__":
    main()
