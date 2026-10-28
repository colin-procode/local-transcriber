# Local Transcriber API

This document covers the Python client and HTTP routes for generating transcripts with a running Local Transcriber instance. All transcription and diarization run on the machine hosting the app.

## Start the service

Start the app and wait for its models to report ready:

```sh
python3 app.py
```

The default API base URL is `http://127.0.0.1:8000`. In WSL, use the URL printed by the app when calling it from Windows. The service returns JSON for API errors in the form `{"error":"message"}`.

## Python client

`transcriber_client.py` uses only the Python standard library. It streams file uploads in 1 MiB blocks, so it does not read a whole media file into memory.

```python
from transcriber_client import LocalTranscriber

client = LocalTranscriber()

status = client.status()
if not status["model"].get("ready"):
    raise RuntimeError(status["model"].get("message", "Transcriber is not ready"))

saved = client.generate_transcript(
    "meeting.mp4",
    title="Project meeting",
    note="Weekly planning",
)

print("Transcript ID:", saved["id"])
for utterance in saved["transcript"]["utterances"]:
    print(utterance["start"], utterance["speaker"], utterance["text"])
```

`generate_transcript()` transcribes the file and saves it to the library by default. The return value is the save response with `id` and `transcript` fields. For a result without saving, use:

```python
result = client.transcribe_file("meeting.mp4", source="system")
print(result["duration"], result["utterances"])
```

To save a result separately, pass a transcript payload to `save_transcript()`. `generate_transcript()` accepts `title`, `note`, `source`, `save`, and `started_at`; its default start time is the current UTC time, and its end time is calculated from the decoded audio duration. Use `source="mic"` or `source="system"` to select the speaker label prefix. `save=False` returns the transcription result without adding a library entry.

The client defaults to `http://127.0.0.1:8000`. Override it with the constructor's `base_url` argument or the `LOCAL_TRANSCRIBER_URL` environment variable. `timeout` is in seconds and defaults to 7200. `TranscriberError` is raised for HTTP, network, or response errors; `FileNotFoundError` is raised when the input file does not exist.

### Command line

The client module can also be run as a command:

```sh
python3 transcriber_client.py meeting.mp4 --title "Project meeting" --note "Weekly planning"
```

Options include `--url`, `--source mic|system`, and `--no-save`. It prints the result as JSON. For example, point a Windows script at the WSL URL printed by the app:

```sh
python3 transcriber_client.py meeting.mp4 --url http://172.26.201.218:8000
```

## HTTP routes

### `GET /api/status`

Returns model readiness and the transcript directory:

```json
{
  "model": {
    "ready": true,
    "runtime": "ready",
    "asr": "Qwen3-ASR 1.7B",
    "diarization": "Nemotron 3 Diarization"
  },
  "transcripts_dir": "/path/to/transcripts"
}
```

During startup, `ready` can be false and `message` describes the current state.

### `POST /api/transcribe-file?filename=meeting.mp4&source=system`

Uploads the media bytes as the request body. Set `Content-Type` to the media type, such as `audio/wav` or `video/mp4`. The server decodes the file with FFmpeg, diarizes it, and transcribes it. The upload limit is 5 GB.

Example with curl:

```sh
curl --fail-with-body \
  -H 'Content-Type: audio/mpeg' \
  --data-binary @meeting.mp3 \
  'http://127.0.0.1:8000/api/transcribe-file?filename=meeting.mp3&source=system'
```

The response contains audio duration, utterances, speaker names, and an optional warning:

```json
{
  "duration": 15.05,
  "utterances": [
    {
      "source": "system",
      "speaker": "COMPUTER_SPEAKER_1",
      "text": "Example recognized speech.",
      "start": 0.2,
      "end": 4.8
    }
  ],
  "speaker_names": {"COMPUTER_SPEAKER_1": "Computer Speaker 1"},
  "warning": ""
}
```

Use `source=mic` to receive `MIC_SPEAKER_*` labels. The endpoint returns the generated result but does not create a transcript library entry.

### `POST /api/transcripts`

Saves a transcript and creates JSON and Markdown files in `transcripts/`. `start_time` and `end_time` are required ISO 8601 timestamps; other fields may be omitted.

```json
{
  "start_time": "2026-10-28T14:00:00+00:00",
  "end_time": "2026-10-28T14:15:05+00:00",
  "title": "Project meeting",
  "note": "Weekly planning",
  "speaker_names": {"COMPUTER_SPEAKER_1": "Computer Speaker 1"},
  "words": [],
  "utterances": [
    {
      "source": "system",
      "speaker": "COMPUTER_SPEAKER_1",
      "text": "Example recognized speech.",
      "start": 0.2,
      "end": 4.8
    }
  ]
}
```

The server returns HTTP 201 with `{"id":"…","transcript":{…}}`.

### Transcript library routes

- `GET /api/transcripts` returns a newest-first list of transcript summaries.
- `GET /api/transcripts/{id}` returns one saved JSON transcript.
- `GET /api/transcripts/{id}/download` returns its Markdown rendering.
- `PATCH /api/transcripts/{id}` accepts `title`, `note`, and/or `speaker_names` fields and returns the updated transcript.
- `DELETE /api/transcripts/{id}` deletes its JSON and Markdown files.

The transcript ID is returned by `POST /api/transcripts` and `LocalTranscriber.generate_transcript()`.
