"""Speech-to-text with faster-whisper (CTranslate2), GPU first with CPU fallback."""

import ctypes
import glob
import logging
import os
import site
import threading

import numpy as np

from jarvis.config import VoiceSettings

log = logging.getLogger(__name__)

MIN_SAMPLES = 16000 // 4  # ignore recordings shorter than 250 ms
_cuda_preloaded = False


def preload_cuda_libraries() -> None:
    """Load cuBLAS/cuDNN from the nvidia-* pip wheels so CTranslate2 can find them by
    soname without the user exporting LD_LIBRARY_PATH."""
    global _cuda_preloaded
    if _cuda_preloaded:
        return
    _cuda_preloaded = True
    patterns = (
        "nvidia/cublas/lib/libcublasLt.so.*",
        "nvidia/cublas/lib/libcublas.so.*",
        "nvidia/cudnn/lib/libcudnn*.so.*",
    )
    for base in site.getsitepackages():
        for pattern in patterns:
            for lib in sorted(glob.glob(os.path.join(base, pattern))):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                except OSError as e:
                    log.debug("Could not preload %s: %s", lib, e)


class SpeechToText:
    def __init__(self, settings: VoiceSettings):
        self.settings = settings
        self._model = None
        self._lock = threading.Lock()
        self.device: str | None = None

    def load(self) -> None:
        """Load the model (blocking; call from a worker thread). Safe to call repeatedly."""
        with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel

            if self.settings.stt_device == "cuda":
                preload_cuda_libraries()
                try:
                    self._model = WhisperModel(
                        self.settings.stt_model, device="cuda",
                        compute_type=self.settings.stt_compute_type,
                    )
                    self.device = "cuda"
                    return
                except Exception as e:  # noqa: BLE001 - any CUDA/driver issue => CPU
                    log.warning("Whisper on CUDA unavailable (%s); falling back to CPU", e)
            self._model = WhisperModel(self.settings.stt_model, device="cpu", compute_type="int8")
            self.device = "cpu"

    def transcribe(self, audio: np.ndarray) -> str:
        """Blocking transcription of 16 kHz mono float32 audio."""
        if audio.size < MIN_SAMPLES:
            return ""
        self.load()
        segments, _info = self._model.transcribe(
            audio.astype(np.float32, copy=False),
            language=self.settings.language or None,
            beam_size=5,
            vad_filter=True,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
