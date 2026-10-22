# Local Transcriber

A small browser-based desktop app for local English transcription. It captures microphone input and computer audio separately, sends both streams to the local model runtime, saves transcript files under `transcripts/`, and lets you browse, title, annotate, and name speakers in previous sessions.

## Setup

On Linux with an NVIDIA GPU, run the setup script once:

```sh
./setup.sh
```

It installs FFmpeg and Python prerequisites when needed, installs NVIDIA's NeMo-Speech.cpp runtime, creates an isolated Qwen/vLLM Python environment, and downloads the Qwen3-ASR 1.7B and Nemotron 3 Diarization models. The setup requires internet access and several gigabytes of free disk space. Model inference runs locally after setup; no API key or hosted transcription service is used.

The app uses Qwen3-ASR 1.7B for live and file transcription, then Nemotron 3 Diarization to add speaker labels when recording stops. For live diarization it temporarily keeps captured PCM in memory; it does not save the audio.

Use **Choose audio or video** and **Transcribe file** for local batch transcription. The app decodes the file with FFmpeg, sends the audio to Qwen as quickly as inference allows, and runs Nemotron diarization. The file does not play in the browser and stays on this computer. FFmpeg must be installed.

## Run

```sh
python3 app.py
```

Open <http://127.0.0.1:8000>. To use a nonstandard runtime location, set `NEMO_SPEECH_BIN` to the full path of the `nemo-speech` executable before starting the app. The app keeps its model service on `127.0.0.1:8766` and the interface on `127.0.0.1:8000`.

When you press **Start recording**, choose the browser tab or window with the audio and enable its audio-sharing option, then allow microphone access. The app keeps the two streams separate and diarizes both independently. Microphone diarization can distinguish multiple voices audible to the mic; if only your voice is audible, it will usually produce one speaker. Some browsers expose tab audio more reliably than audio from arbitrary desktop applications.

Press **Stop recording** to save a JSON transcript and a readable Markdown copy in `transcripts/`. Each filename includes both the start and stop date/time. Use the library panel to reopen a transcript, edit its title or notes, and replace generated speaker labels with names.

## Notes

- The browser must support `getDisplayMedia` audio capture and microphone access. Localhost is used so the browser can grant capture permissions. For YouTube or another browser tab, select the tab in the share dialog and enable its audio option; screen or window sharing may provide no audio track.
- Audio is sent only to local model runtimes on this computer. The app does not persist audio recordings.
- The diarizer assigns generic speaker IDs separately for microphone and computer audio. It does not know real names until you enter them, and speaker IDs across those two audio streams are not identity-matched.
- First launch needs internet access to fetch model artifacts. Once cached, model inference is local.
