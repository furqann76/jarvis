"""Always-on listening for hands-free mode: energy-based speech segmentation.

Each detected utterance (speech bounded by silence) is handed to a callback as 16 kHz
float32 audio; the caller transcribes it and checks for the wake word (voice/wake.py).
"""

import collections
import logging
import shutil
import subprocess
import threading
from collections.abc import Callable

import numpy as np

from jarvis.voice.microphone import SAMPLE_RATE, MicrophoneError, pcm16_to_float

log = logging.getLogger(__name__)

FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2


class SpeechSegmenter:
    """Pure segmentation logic (separate from audio I/O so it can be unit-tested)."""

    def __init__(
        self,
        on_utterance: Callable[[np.ndarray], None],
        min_rms: float = 0.012,
        noise_ratio: float = 2.5,
        calibration_ms: int = 600,
        start_frames: int = 3,
        end_silence_ms: int = 800,
        preroll_ms: int = 300,
        min_utterance_ms: int = 400,
        max_utterance_s: float = 15.0,
    ):
        self.on_utterance = on_utterance
        self.min_rms = min_rms
        self.noise_ratio = noise_ratio
        self.start_frames = start_frames
        self.end_frames = end_silence_ms // FRAME_MS
        self.min_frames = min_utterance_ms // FRAME_MS
        self.max_frames = int(max_utterance_s * 1000 // FRAME_MS)
        self.noise = min_rms / 2
        # The first frames measure the room's background level before detecting speech:
        # a fixed threshold misfires in noisy rooms (fans, hum) and never adapts.
        self._calibration: list[float] = []
        self.calibration_frames = calibration_ms // FRAME_MS
        self._preroll: collections.deque = collections.deque(maxlen=preroll_ms // FRAME_MS)
        self._speech: list[np.ndarray] = []
        self._loud_run = 0
        self._silent_run = 0
        self.in_speech = False

    def threshold(self) -> float:
        return max(self.min_rms, self.noise * self.noise_ratio)

    def reset(self) -> None:
        self._preroll.clear()
        self._speech = []
        self._loud_run = self._silent_run = 0
        self.in_speech = False

    def feed(self, frame: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(frame * frame))) if frame.size else 0.0
        if len(self._calibration) < self.calibration_frames:
            self._calibration.append(rms)
            if len(self._calibration) == self.calibration_frames:
                self.noise = max(self.noise, float(np.median(self._calibration)))
            return
        loud = rms > self.threshold()
        if not self.in_speech:
            if not loud:  # adapt the noise floor only on background audio
                self.noise = 0.95 * self.noise + 0.05 * rms
            self._preroll.append(frame)
            self._loud_run = self._loud_run + 1 if loud else 0
            if self._loud_run >= self.start_frames:
                self.in_speech = True
                self._speech = list(self._preroll)
                self._silent_run = 0
            return
        self._speech.append(frame)
        self._silent_run = 0 if loud else self._silent_run + 1
        if self._silent_run >= self.end_frames or len(self._speech) >= self.max_frames:
            frames, trailing_silence = self._speech, self._silent_run
            if len(frames) >= self.max_frames and trailing_silence == 0:
                # "Speech" that never pauses is usually a new steady background noise:
                # raise the floor towards its level so detection recovers.
                level = float(np.sqrt(np.mean(np.concatenate(frames) ** 2)))
                self.noise = max(self.noise, level / self.noise_ratio * 1.05)
            self.reset()
            if len(frames) - trailing_silence >= self.min_frames:
                self.on_utterance(np.concatenate(frames))


class HandsFreeListener:
    def __init__(
        self, on_utterance: Callable[[np.ndarray], None], command: list[str] | None = None
    ):
        self.segmenter = SpeechSegmenter(on_utterance)
        self._command = command or [
            "pw-record", "--raw", "--rate", str(SAMPLE_RATE), "--channels", "1",
            "--format", "s16", "-",
        ]
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._paused = threading.Event()

    @property
    def running(self) -> bool:
        return self._proc is not None

    def start(self) -> None:
        if self._proc is not None:
            return
        if shutil.which(self._command[0]) is None:
            raise MicrophoneError(f"{self._command[0]} not found (install pipewire-bin)")
        self._proc = subprocess.Popen(self._command, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        self._thread = threading.Thread(target=self._run, daemon=True, name="hands-free")
        self._thread.start()

    def _run(self) -> None:
        proc = self._proc
        assert proc and proc.stdout
        while True:
            raw = proc.stdout.read(FRAME_BYTES)
            if not raw or len(raw) < FRAME_BYTES:
                break
            if self._paused.is_set():
                continue
            try:
                self.segmenter.feed(pcm16_to_float(raw))
            except Exception:  # noqa: BLE001 - a callback bug must not kill listening
                log.exception("Utterance handler failed")

    def pause(self) -> None:
        """Ignore audio (e.g. while Jarvis is speaking, so it doesn't hear itself)."""
        self._paused.set()
        self.segmenter.reset()

    def resume(self) -> None:
        self.segmenter.reset()
        self._paused.clear()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread:
            self._thread.join(timeout=2)
