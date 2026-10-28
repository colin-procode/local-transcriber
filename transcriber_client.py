#!/usr/bin/env python3
"""Python client for the local-transcriber HTTP API."""

from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class TranscriberError(RuntimeError):
    """Raised when the local transcriber API returns an error."""


class LocalTranscriber:
    """Call the local transcription service and save results to its library."""

    def __init__(self, base_url: str | None = None, timeout: float = 7200) -> None:
        self.base_url = (base_url or os.environ.get("LOCAL_TRANSCRIBER_URL") or "http://127.0.0.1:8000").rstrip("/")
        self.timeout = timeout
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an HTTP or HTTPS URL")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("base_url must contain only the scheme, host, and optional port")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _decode_json(status: int, body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TranscriberError(f"Transcriber returned invalid JSON (HTTP {status})") from exc
        if status < 200 or status >= 300:
            message = data.get("error", f"HTTP {status}") if isinstance(data, dict) else f"HTTP {status}"
            raise TranscriberError(str(message))
        if not isinstance(data, dict):
            raise TranscriberError("Transcriber returned an unexpected JSON value")
        return data

    def _json_request(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {} if body is None else {"Content-Type": "application/json"}
        request = Request(self._url(path), data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return self._decode_json(response.status, response.read())
        except HTTPError as exc:
            raise self._error_from_http(exc) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TranscriberError(f"Could not reach local transcriber at {self.base_url}: {exc}") from exc

    @classmethod
    def _error_from_http(cls, exc: HTTPError) -> TranscriberError:
        try:
            data = json.loads(exc.read().decode("utf-8"))
            message = data.get("error", f"HTTP {exc.code}") if isinstance(data, dict) else f"HTTP {exc.code}"
        except (UnicodeDecodeError, json.JSONDecodeError):
            message = f"HTTP {exc.code}: {exc.reason}"
        return TranscriberError(str(message))

    def status(self) -> dict[str, Any]:
        """Return runtime readiness and the transcripts directory."""
        return self._json_request("/api/status")

    def transcribe_file(self, path: str | Path, source: str = "system") -> dict[str, Any]:
        """Transcribe a local audio/video file and return utterances and speakers."""
        file_path = Path(path).expanduser()
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        if source not in {"mic", "system"}:
            raise ValueError("source must be 'mic' or 'system'")

        query = urlencode({"filename": file_path.name, "source": source})
        endpoint = f"/api/transcribe-file?{query}"
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        connection_type = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        connection = connection_type(self._host, self._port, timeout=self.timeout)
        try:
            connection.putrequest("POST", endpoint)
            connection.putheader("Content-Type", content_type)
            connection.putheader("Content-Length", str(file_path.stat().st_size))
            connection.endheaders()
            with file_path.open("rb") as audio_file:
                while chunk := audio_file.read(1024 * 1024):
                    connection.send(chunk)
            response = connection.getresponse()
            return self._decode_json(response.status, response.read())
        except HTTPError as exc:
            raise self._error_from_http(exc) from exc
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise TranscriberError(f"Could not upload {file_path} to {self.base_url}: {exc}") from exc
        finally:
            connection.close()

    def save_transcript(self, transcript: dict[str, Any]) -> dict[str, Any]:
        """Save a transcript payload to the local library."""
        return self._json_request("/api/transcripts", method="POST", payload=transcript)

    def generate_transcript(
        self,
        path: str | Path,
        *,
        title: str | None = None,
        note: str = "",
        source: str = "system",
        save: bool = True,
        started_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Transcribe a file and, by default, save it in the transcript library."""
        started_at = started_at or datetime.now(timezone.utc)
        result = self.transcribe_file(path, source=source)
        if not save:
            return result

        duration = max(0.0, float(result.get("duration", 0)))
        transcript = {
            "start_time": started_at.isoformat(),
            "end_time": (started_at + timedelta(seconds=duration)).isoformat(),
            "title": title or Path(path).stem or "Untitled transcript",
            "note": note,
            "speaker_names": result.get("speaker_names", {}),
            "words": [],
            "utterances": result.get("utterances", []),
            "duration": duration,
            "warning": result.get("warning", ""),
        }
        return self.save_transcript(transcript)


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe media through the local-transcriber app")
    parser.add_argument("file", type=Path, help="Audio or video file to transcribe")
    parser.add_argument("--url", default=None, help="App URL (default: LOCAL_TRANSCRIBER_URL or http://127.0.0.1:8000)")
    parser.add_argument("--title", default=None, help="Title for the saved transcript")
    parser.add_argument("--note", default="", help="Note to save with the transcript")
    parser.add_argument("--source", choices=("mic", "system"), default="system")
    parser.add_argument("--no-save", action="store_true", help="Return the transcript without saving it")
    args = parser.parse_args()

    try:
        client = LocalTranscriber(base_url=args.url)
        result = client.generate_transcript(
            args.file,
            title=args.title,
            note=args.note,
            source=args.source,
            save=not args.no_save,
        )
    except (FileNotFoundError, ValueError, TranscriberError) as exc:
        print(f"transcriber: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
