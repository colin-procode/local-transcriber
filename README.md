# Local Transcriber

A small browser-based desktop app for local English transcription. It captures microphone input and computer audio separately, sends both streams to the local model runtime, saves transcript files under `transcripts/`, and lets you browse, title, annotate, and name speakers in previous sessions.

## Setup

On Linux with an NVIDIA GPU, run the setup script once:

```sh
./setup.sh
```

It installs FFmpeg and Python prerequisites when needed, installs NVIDIA's NeMo-Speech.cpp runtime, creates an isolated Qwen/vLLM Python environment, and downloads the Qwen3-ASR 1.7B and Nemotron 3 Diarization models. The setup requires internet access and several gigabytes of free disk space. Model inference runs locally after setup; no API key or hosted transcription service is used.

On Ubuntu 22.04 in WSL with a supported AMD Radeon GPU, first install AMD's [ROCm 6.4.2 WSL stack](https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.1/docs/install/installrad/wsl/install-radeon.html) and confirm `rocminfo` lists the GPU, then run `./setup.sh`. This installs AMD's WSL-compatible PyTorch build and uses Qwen's Transformers backend for local transcription. The app detects the ROCm backend automatically.

The app captures microphone and computer audio separately, then runs Qwen3-ASR 1.7B and Nemotron 3 Diarization after you stop recording. Uploaded files use the same local transcription and diarization pipeline. Captured PCM stays in memory until processing; the app does not save audio files.

Use **Choose audio or video** and **Transcribe file** for local batch transcription. The app decodes the file with FFmpeg, sends the audio to Qwen as quickly as inference allows, and runs Nemotron diarization. The file does not play in the browser and stays on this computer. FFmpeg must be installed.

## Python API

`transcriber_client.py` lets Python programs send a local media file to the running app. `generate_transcript()` transcribes the file and saves the result to the transcript library; `transcribe_file()` returns the result without saving it.

See [API.md](API.md) for client options, HTTP request and response formats, and transcript-library routes.

```python
from transcriber_client import LocalTranscriber

client = LocalTranscriber()  # http://127.0.0.1:8000
print(client.status())

saved = client.generate_transcript("meeting.mp4", title="Project meeting")
print(saved["id"])
print(saved["transcript"]["utterances"])
```

The same workflow is available from the command line:

```sh
python3 transcriber_client.py meeting.mp4 --title "Project meeting"
```

Set `LOCAL_TRANSCRIBER_URL` or pass `base_url`/`--url` to use another app address. Use `save=False` or `--no-save` to return the transcription without adding it to the library. The HTTP routes used by the client are `GET /api/status`, `POST /api/transcribe-file`, and `POST /api/transcripts`.

## Run

```sh
python3 app.py
```

On standard Linux, the app listens on `127.0.0.1`. In WSL, it detects the private WSL network address and prints the matching URL when it starts; open that printed URL in the Windows browser. The Qwen live-transcription socket uses the same interface. Set `TRANSCRIBER_HOST` or `TRANSCRIBER_QWEN_HOST` to override the detected address.

When you press **Start recording**, choose the browser tab or window with the audio and enable its audio-sharing option, then allow microphone access. The app keeps the two streams separate and diarizes both independently. Microphone diarization can distinguish multiple voices audible to the mic; if only your voice is audible, it will usually produce one speaker. Some browsers expose tab audio more reliably than audio from arbitrary desktop applications.

Press **Stop recording** to save a JSON transcript and a readable Markdown copy in `transcripts/`. Each filename includes both the start and stop date/time. Use the library panel to reopen a transcript, edit its title or notes, and replace generated speaker labels with names.

## Notes

- The browser must support `getDisplayMedia` audio capture and microphone access. Localhost is used so the browser can grant capture permissions. For YouTube or another browser tab, select the tab in the share dialog and enable its audio option; screen or window sharing may provide no audio track.
- Audio is sent only to local model runtimes on this computer. The app does not persist audio recordings.
- The diarizer assigns generic speaker IDs separately for microphone and computer audio. It does not know real names until you enter them, and speaker IDs across those two audio streams are not identity-matched.
- First launch needs internet access to fetch model artifacts. Once cached, model inference is local.
