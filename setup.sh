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
  fail "This setup script targets Linux with a supported NVIDIA GPU or WSL ROCm installation."
fi
GPU_BACKEND=""
ROCM_GPU_INFO=""
if command -v rocminfo >/dev/null 2>&1; then
  ROCM_GPU_INFO="$(rocminfo 2>/dev/null || true)"
fi
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_BACKEND="nvidia"
elif [[ -e /dev/dxg ]] && grep -Eq 'Name:[[:space:]]+gfx[0-9a-f]+' <<<"$ROCM_GPU_INFO"; then
  GPU_BACKEND="amd-rocm-wsl"
else
  fail "A supported NVIDIA GPU or AMD ROCm WSL installation is required."
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

if [[ "$GPU_BACKEND" == "amd-rocm-wsl" ]]; then
  python3 - <<'PY'
import sys
if sys.version_info[:2] != (3, 10):
    raise SystemExit("AMD ROCm 6.4.2 WSL PyTorch wheels require Python 3.10 in Ubuntu 22.04")
PY
  if [[ ! -e /opt/rocm-6.4.2/lib/libhsa-runtime64.so.1 ]]; then
    fail "ROCm 6.4.2 WSL runtime is missing. Install AMD's WSL ROCm 6.4.2 packages, then rerun setup.sh."
  fi
fi

mkdir -p "$RUNTIME_DIR"
export PATH="$HOME/.local/bin:$PATH"

printf 'Installing NeMo-Speech.cpp %s…\n' "$NEMO_SPEECH_VERSION"
curl -fsSL https://github.com/NVIDIA/NeMo-Speech.cpp/raw/main/scripts/install.sh \
  | sh -s -- --version "$NEMO_SPEECH_VERSION"

printf 'Creating the Qwen ASR environment…\n'
python3 -m venv "$QWEN_VENV"
"$QWEN_VENV/bin/python" -m pip install --upgrade pip
if [[ "$GPU_BACKEND" == "amd-rocm-wsl" ]]; then
  ROCM_WHEEL_BASE="https://repo.radeon.com/rocm/manylinux/rocm-rel-6.4.2"
  "$QWEN_VENV/bin/python" -m pip install \
    "$ROCM_WHEEL_BASE/torch-2.6.0%2Brocm6.4.2.git76481f7c-cp310-cp310-linux_x86_64.whl" \
    "$ROCM_WHEEL_BASE/torchaudio-2.6.0%2Brocm6.4.2.gitd8831425-cp310-cp310-linux_x86_64.whl" \
    "$ROCM_WHEEL_BASE/pytorch_triton_rocm-3.2.0%2Brocm6.4.2.git7e948ebf-cp310-cp310-linux_x86_64.whl" \
    "numpy==1.26.4"
  rm -f "$QWEN_VENV/lib/python3.10/site-packages/torch/lib/libhsa-runtime64.so"*
  "$QWEN_VENV/bin/python" -m pip install qwen-asr huggingface_hub websockets
else
  "$QWEN_VENV/bin/python" -m pip install "qwen-asr[vllm]" huggingface_hub numpy websockets
fi

printf 'Downloading Qwen3-ASR 1.7B model weights…\n'
"$QWEN_VENV/bin/python" - <<'PY'
from huggingface_hub import snapshot_download

snapshot_download(repo_id="Qwen/Qwen3-ASR-1.7B")
PY

printf 'Downloading Nemotron 3 diarization model…\n'
nemo-speech pull nvidia/Nemotron-3-Diarization

printf '\nSetup complete. Start the app with:\n  cd %q\n  python3 app.py\n' "$ROOT_DIR"
printf 'Open the URL printed by python3 app.py after the models finish loading. On WSL, it will use the WSL private IP so a Windows browser can reach it.\n'
