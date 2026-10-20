"""Text-to-speech with Piper (ONNX, CPU), played through PipeWire's ``pw-play``."""

import re
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

from jarvis.config import VoiceSettings

_MARKDOWN = [
    (re.compile(r"```.*?```", re.DOTALL), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__"), r"\1\2"),
    (re.compile(r"^\s*[-*•]\s+", re.MULTILINE), ""),
    (re.compile(r"^\s*#+\s*", re.MULTILINE), ""),
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
]
_LONG_PATH = re.compile(r"(/[\w.\-]+){3,}/?")
_OWN_NAME = re.compile(r"\b(hey\s+)?jarvis\b[,:]?\s*", re.IGNORECASE)


class TextToSpeechError(Exception):
    pass


def to_speakable(text: str, max_chars: int) -> str:
    """Strip markdown and shorten long paths so replies sound natural."""
    for pattern, repl in _MARKDOWN:
        text = pattern.sub(repl, text)
    # Never say the wake word aloud: the wake listener stays on while we speak.
    text = _OWN_NAME.sub("", text)
    text = _LONG_PATH.sub(lambda m: Path(m.group(0)).name or m.group(0), text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: end + 1] if end > max_chars // 2 else cut + "…"
    return text


class TextToSpeech:
    def __init__(self, settings: VoiceSettings, player: list[str] | None = None):
        self.settings = settings
        self.voice_path = Path(settings.tts_voice).expanduser()
        self._player = player or ["pw-play"]
        self._voice = None
        self._lock = threading.Lock()
        self._playback: subprocess.Popen | None = None
        self._generation = 0  # bumped by stop(); a speak() from an older generation is void

    def load(self) -> None:
        with self._lock:
            if self._voice is not None:
                return
            if self.settings.tts_engine == "kokoro":
                self._voice = self._load_kokoro()
                return
            if not self.voice_path.is_file():
                raise TextToSpeechError(
                    f"Piper voice not found: {self.voice_path}. Run scripts/setup_env.sh"
                )
            from piper import PiperVoice

            self._voice = PiperVoice.load(self.voice_path)

    def _load_kokoro(self):
        model_dir = Path(self.settings.kokoro_model_dir).expanduser()
        model, voices = model_dir / "kokoro-v1.0.onnx", model_dir / "voices-v1.0.bin"
        if not (model.is_file() and voices.is_file()):
            raise TextToSpeechError(f"Kokoro model not found in {model_dir}. Run "
                                    "scripts/setup_env.sh")
        from kokoro_onnx import Kokoro

        return Kokoro(str(model), str(voices))

    def _kokoro_voice(self):
        """A voice name ("bm_george") or a weighted blend ("bm_george:0.6,bm_lewis:0.4")."""
        spec = self.settings.kokoro_voice
        if "," not in spec and ":" not in spec:
            return spec
        import numpy as np

        style = None
        for part in spec.split(","):
            name, _, weight = part.strip().partition(":")
            vec = self._voice.get_voice_style(name) * float(weight or 1)
            style = vec if style is None else np.add(style, vec)
        return style

    def synthesize_to_file(self, text: str, path: Path) -> None:
        self.load()
        if self.settings.tts_engine == "kokoro":
            import numpy as np

            audio, rate = self._voice.create(text, voice=self._kokoro_voice(),
                                             speed=self.settings.kokoro_speed, lang="en-gb")
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(pcm.tobytes())
            return
        with wave.open(str(path), "wb") as wav:
            self._voice.synthesize_wav(text, wav)

    def speak(self, text: str) -> None:
        """Blocking: synthesize and play. Call from a worker thread."""
        speakable = to_speakable(text, self.settings.max_spoken_chars)
        if not speakable:
            return
        self.stop()
        generation = self._generation
        with tempfile.NamedTemporaryFile(suffix=".wav", prefix="jarvis-tts-") as tmp:
            self.synthesize_to_file(speakable, Path(tmp.name))
            if generation != self._generation:
                return  # interrupted while synthesizing
            proc = subprocess.Popen(
                [*self._player, tmp.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._playback = proc
            proc.wait()
            if self._playback is proc:
                self._playback = None

    def stop(self) -> None:
        self._generation += 1
        proc = self._playback
        if proc and proc.poll() is None:
            proc.terminate()
