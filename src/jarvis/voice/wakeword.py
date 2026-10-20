"""Always-on wake-word detection logic ("Hey Jarvis" / "Jarvis").

A cheap wake-word model (openWakeWord, CPU) scores every 80 ms chunk; nothing is
transcribed until it fires. After a trigger the detector keeps recording until the speaker
pauses, so "Jarvis, open the calculator" arrives as one clip containing the command.

Two thresholds: a score >= `high` is a confident wake (the caller plays a chime at once);
`low` <= score < `high` is a candidate the caller must confirm by transcribing the clip
(fast "Jarvis, do X" phrasing often scores low).
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280  # 80 ms: openWakeWord's native frame


@dataclass
class WakeClip:
    audio: np.ndarray  # float32 mono 16 kHz, starting shortly before the wake word
    peak_score: float
    confident: bool  # peak_score >= high threshold


class WakeDetector:
    def __init__(
        self,
        scorer: Callable[[np.ndarray], float],
        on_clip: Callable[[WakeClip], None],
        on_confident: Callable[[], None] | None = None,
        high: float = 0.5,
        low: float = 0.15,
        preroll_s: float = 2.0,
        end_silence_s: float = 0.9,
        wait_for_command_s: float = 4.0,
        max_clip_s: float = 12.0,
        min_rms: float = 0.012,
    ):
        self.scorer = scorer
        self.on_clip = on_clip
        self.on_confident = on_confident
        self.high, self.low = high, low
        chunk_s = CHUNK_SAMPLES / SAMPLE_RATE
        self._preroll: deque[np.ndarray] = deque(maxlen=int(preroll_s / chunk_s))
        self.end_silence_chunks = int(end_silence_s / chunk_s)
        self.wait_chunks = int(wait_for_command_s / chunk_s)
        self.max_chunks = int(max_clip_s / chunk_s)
        self.min_rms = min_rms
        self.noise = min_rms / 2
        # Background level = a low percentile of the last ~3 s while idle. (Adapting only
        # on "quiet" chunks never converges in a room louder than the initial guess.)
        self._recent_rms: deque[float] = deque(maxlen=int(3.0 / chunk_s))
        self.paused = False
        # While Jarvis itself is talking, only a confident wake word counts (borderline
        # scores could be its own voice); lets the user interrupt it.
        self.strict = False
        self._reset_capture()

    def _reset_capture(self) -> None:
        self.capturing = False
        self._clip: list[np.ndarray] = []
        self._peak = 0.0
        self._confident_announced = False
        self._heard_speech_after = False  # speech after the wake word (the command)
        self._quiet_run = 0
        self._since_trigger = 0

    def _loud(self, rms: float) -> bool:
        return rms > max(self.min_rms, self.noise * 2.5)

    def feed(self, chunk_int16: np.ndarray) -> None:
        """Feed one 80 ms chunk of 16 kHz int16 audio."""
        audio = chunk_int16.astype(np.float32) / 32768.0
        if self.paused:
            self._preroll.clear()
            self._reset_capture()
            return
        rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0
        score = float(self.scorer(chunk_int16))

        if not self.capturing:
            self._recent_rms.append(rms)
            self.noise = float(np.percentile(self._recent_rms, 20))
            self._preroll.append(audio)
            if score >= (self.high if self.strict else self.low):
                self.capturing = True
                self._clip = list(self._preroll)
                self._preroll.clear()
                self._peak = score
                self._maybe_announce()
            return

        self._clip.append(audio)
        self._since_trigger += 1
        self._peak = max(self._peak, score)
        self._maybe_announce()
        if score >= self.low:
            self._quiet_run = 0  # still saying the wake phrase
            return
        if self._loud(rms):
            self._heard_speech_after = True
            self._quiet_run = 0
        else:
            self._quiet_run += 1
        done = (
            (self._heard_speech_after and self._quiet_run >= self.end_silence_chunks)
            or (not self._heard_speech_after and self._since_trigger >= self.wait_chunks)
            or len(self._clip) >= self.max_chunks
        )
        if done:
            clip = WakeClip(np.concatenate(self._clip), self._peak, self._peak >= self.high)
            self._reset_capture()
            self.on_clip(clip)

    def _maybe_announce(self) -> None:
        if self._peak >= self.high and not self._confident_announced:
            self._confident_announced = True
            if self.on_confident:
                self.on_confident()
