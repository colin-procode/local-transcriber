#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
RUNTIME_DIR="$DATA_HOME/transcriber"
QWEN_VENV="$RUNTIME_DIR/qwen-venv"
NEMO_SPEECH_VERSION="0.2.0"

fail() {
  printf 'setup: %s\n' "$*" >&2
  exit 1
}

if [[ "$(uname -s)" != "Linux" ]]; then
  fail "This setup script targets Linux with an NVIDIA GPU and vLLM."
fi
if ! command -v nvidia-smi >/dev/null 2>&1; then
  fail "An NVIDIA GPU and working NVIDIA driver (nvidia-smi) are required for Qwen/vLLM."
fi

if ! command -v apt-get >/dev/null 2>&1; then
  fail "Install Python 3, python3-venv, curl, and FFmpeg with your Linux package manager, then rerun setup.sh."
fi

missing_packages=()
command -v python3 >/dev/null 2>&1 || missing_packages+=(python3)
command -v curl >/dev/null 2>&1 || missing_packages+=(curl)
command -v ffmpeg >/dev/null 2>&1 || missing_packages+=(ffmpeg)
python3 -c 'import venv' >/dev/null 2>&1 || missing_packages+=(python3-venv)

if ((${#missing_packages[@]})); then
  if [[ "$EUID" -eq 0 ]]; then
    apt-get update
    apt-get install -y "${missing_packages[@]}"
  elif command -v sudo >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y "${missing_packages[@]}"
  else
    fail "Missing packages: ${missing_packages[*]}. Install them with apt, then rerun setup.sh."
  fi
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required")
PY

mkdir -p "$RUNTIME_DIR"
export PATH="$HOME/.local/bin:$PATH"

printf 'Installing NeMo-Speech.cpp %s…\n' "$NEMO_SPEECH_VERSION"
curl -fsSL https://github.com/NVIDIA/NeMo-Speech.cpp/raw/main/scripts/install.sh \
  | sh -s -- --version "$NEMO_SPEECH_VERSION"

printf 'Creating the Qwen ASR environment…\n'
python3 -m venv "$QWEN_VENV"
"$QWEN_VENV/bin/python" -m pip install --upgrade pip
"$QWEN_VENV/bin/python" -m pip install "qwen-asr[vllm]" huggingface_hub numpy websockets

printf 'Downloading Qwen3-ASR 1.7B model weights…\n'
"$QWEN_VENV/bin/python" - <<'PY'
from huggingface_hub import snapshot_download

snapshot_download(repo_id="Qwen/Qwen3-ASR-1.7B")
PY

printf 'Downloading Nemotron 3 diarization model…\n'
nemo-speech pull nvidia/Nemotron-3-Diarization

printf '\nSetup complete. Start the app with:\n  cd %q\n  python3 app.py\n' "$ROOT_DIR"
printf 'Open http://127.0.0.1:8000 after the models finish loading.\n'
