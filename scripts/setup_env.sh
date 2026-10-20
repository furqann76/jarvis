#!/usr/bin/env bash
# Jarvis environment setup: Python 3.12 venv, Python deps (GPU speech), Chromium for
# Playwright, Piper voice, Ollama models. Needs no sudo; prints what is missing.
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3.12}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/jarvis"

if ! command -v "$PYTHON" >/dev/null; then
    echo "error: $PYTHON not found (sudo apt install python3.12 python3.12-venv)" >&2
    exit 1
fi

[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
if command -v nvidia-smi >/dev/null; then
    .venv/bin/python -m pip install -e ".[dev,gpu]"
else
    .venv/bin/python -m pip install -e ".[dev]"
fi

echo "== Browser for automation"
.venv/bin/playwright install chromium

echo "== Piper voice"
mkdir -p "$DATA/voices"
[ -f "$DATA/voices/en_US-lessac-medium.onnx" ] || \
    .venv/bin/python -m piper.download_voices en_US-lessac-medium --download-dir "$DATA/voices"

echo "== Kokoro voices (Jarvis-style British voices)"
mkdir -p "$DATA/voices/kokoro"
for f in kokoro-v1.0.onnx voices-v1.0.bin; do
    [ -s "$DATA/voices/kokoro/$f" ] || curl -sSL -o "$DATA/voices/kokoro/$f" \
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f"
done

mkdir -p /tmp/jarvis_workspace

echo "== System tools"
for tool in pw-record pw-play gio gst-launch-1.0; do
    command -v "$tool" >/dev/null || echo "missing: $tool (pipewire-bin / libglib2.0-bin / gstreamer1.0-tools)"
done
gst-inspect-1.0 pipewiresrc >/dev/null 2>&1 || echo "missing: GStreamer pipewiresrc (gstreamer1.0-pipewire)"

echo "== Ollama models"
if command -v ollama >/dev/null; then
    for model in $(sed -n 's/^\(model\|vision_model\) *= *"\(.*\)".*/\2/p' config/settings.toml); do
        if ! ollama list | awk 'NR>1 {print $1}' | grep -qx "$model"; then
            echo "pulling $model"; ollama pull "$model"
        fi
    done
else
    echo "warning: ollama not installed. See https://ollama.com/download" >&2
fi

echo "== Window activation helper (libxcb-cursor0, unpacked locally if missing)"
if ! ldconfig -p | grep -q libxcb-cursor.so.0; then
    XCB="$DATA/xcb-libs"; mkdir -p "$XCB/deb"
    (cd "$XCB/deb" && apt download libxcb-cursor0 && dpkg -x libxcb-cursor0_*.deb "$XCB/root")
fi

echo "== Wake word: start at login"
.venv/bin/python -m jarvis.daemon --install

echo "Done. Say “Hey Jarvis”, or run: .venv/bin/python -m jarvis.main"
echo "First keyboard/mouse use shows a GNOME 'Remote desktop' dialog: allow it once."
