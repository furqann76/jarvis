"""Push-to-talk microphone capture through PipeWire's ``pw-record`` (no PortAudio needed)."""

import shutil
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000  # what Whisper expects
_BYTES_PER_SAMPLE = 2


class MicrophoneError(Exception):
    pass


class Microphone:
    def __init__(self, max_seconds: float = 30.0, command: list[str] | None = None):
        self.max_seconds = max_seconds
        self._command = command or [
            "pw-record", "--raw", "--rate", str(SAMPLE_RATE), "--channels", "1",
            "--format", "s16", "-",
        ]
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        return self._proc is not None

    def start(self) -> None:
        if self._proc is not None:
            raise MicrophoneError("Already recording")
        if shutil.which(self._command[0]) is None:
            raise MicrophoneError(f"{self._command[0]} not found (install pipewire-bin)")
        self._chunks = []
        self._proc = subprocess.Popen(
            self._command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        # The reader gets its own reference: stop() clears self._proc while it may still run.
        self._reader = threading.Thread(target=self._read, args=(self._proc,), daemon=True,
                                        name="mic-reader")
        self._reader.start()

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout
        limit = int(self.max_seconds * SAMPLE_RATE * _BYTES_PER_SAMPLE)
        total = 0
        while total < limit:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            with self._lock:
                self._chunks.append(chunk)
            total += len(chunk)
        if total >= limit:  # hit max duration: stop the recorder ourselves
            proc.terminate()

    def stop(self) -> np.ndarray:
        """Stop recording and return mono float32 samples in [-1, 1] at 16 kHz."""
        if self._proc is None:
            raise MicrophoneError("Not recording")
        proc, self._proc = self._proc, None
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        if self._reader:
            self._reader.join(timeout=2)
        with self._lock:
            raw = b"".join(self._chunks)
        raw = raw[: len(raw) - len(raw) % _BYTES_PER_SAMPLE]
        return pcm16_to_float(raw)


def pcm16_to_float(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def load_wav(path: str | Path) -> np.ndarray:
    """Read a 16 kHz mono 16-bit WAV (as written by the wake daemon) to float32."""
    with wave.open(str(path)) as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise wave.Error("expected 16 kHz mono 16-bit audio")
        return pcm16_to_float(w.readframes(w.getnframes()))
