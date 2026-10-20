"""Always-on wake-word listener. Starts at login and opens/wakes the Jarvis GUI.

    python -m jarvis.daemon              # run in the foreground
    python -m jarvis.daemon --install    # start at login (XDG autostart) + app launcher, start now
    python -m jarvis.daemon --uninstall

Privacy: audio is scored locally by the wake-word model and discarded; it is only
transcribed (locally) when the wake word fires.
"""

import argparse
import fcntl
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from jarvis.ipc import runtime_dir, send_to_gui, speaking_flag
from jarvis.voice.wake import parse_wake
from jarvis.voice.wakeword import CHUNK_SAMPLES, SAMPLE_RATE, WakeClip, WakeDetector

log = logging.getLogger("jarvis.daemon")

PROJECT_DIR = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
CHIME = "/usr/share/sounds/freedesktop/stereo/message.oga"
AUTOSTART_FILE = Path("~/.config/autostart/jarvis-wake.desktop").expanduser()
LAUNCHER_FILE = Path("~/.local/share/applications/jarvis.desktop").expanduser()
LOG_FILE = Path("~/.local/state/jarvis/wake.log").expanduser()
RECORD_CMD = ["pw-record", "--raw", "--rate", str(SAMPLE_RATE), "--channels", "1",
              "--format", "s16", "-"]


def load_wake_scorer():
    import openwakeword
    from openwakeword.model import Model

    model = Model(wakeword_model_paths=[openwakeword.models["hey_jarvis"]["model_path"]])
    key = next(iter(model.models))
    return lambda chunk: model.predict(chunk)[key]


class Verifier:
    """Small CPU Whisper used only to confirm borderline wake-word triggers."""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()

    def heard_wake_word(self, audio: np.ndarray) -> bool:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel("base.en", device="cpu", compute_type="int8")
            segments, _ = self._model.transcribe(audio, language="en", beam_size=1)
            text = " ".join(s.text.strip() for s in segments)
        woke, _ = parse_wake(text)
        log.info("verify: %s", "wake" if woke else "no wake")
        return woke


# Children we started (GUI, chime players). Polled periodically so finished ones are reaped
# instead of lingering as zombie processes.
_children: list[subprocess.Popen] = []


def reap_children() -> None:
    _children[:] = [c for c in _children if c.poll() is None]


def play_chime() -> None:
    if Path(CHIME).exists() and shutil.which("pw-play"):
        _children.append(subprocess.Popen(["pw-play", CHIME], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL))


def save_clip(audio: np.ndarray) -> Path:
    fd, name = tempfile.mkstemp(prefix="wake-", suffix=".wav", dir=runtime_dir())
    os.close(fd)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(name, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return Path(name)


GUI_STARTUP_S = 30.0
_gui_spawned_at = 0.0


def open_gui(clip_path: Path | None) -> None:
    global _gui_spawned_at
    message = {"action": "wake", "audio": str(clip_path) if clip_path else None}
    # If we just launched the GUI it may still be starting up: wait for it rather than
    # launching a second window.
    while True:
        if send_to_gui(message):
            log.info("woke running GUI")
            return
        if time.monotonic() - _gui_spawned_at > GUI_STARTUP_S:
            break
        time.sleep(1.0)
    _gui_spawned_at = time.monotonic()
    log.info("starting GUI")
    args = [PYTHON, "-m", "jarvis.main"]
    if clip_path:
        args += ["--wake-audio", str(clip_path)]
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE.with_name("gui.log"), "ab") as out:
        _children.append(subprocess.Popen(args, cwd=PROJECT_DIR, stdout=out, stderr=out,
                                          stdin=subprocess.DEVNULL, start_new_session=True))


class WakeService:
    def __init__(self) -> None:
        self.verifier = Verifier()
        self.detector = WakeDetector(load_wake_scorer(), self._on_clip, self._on_confident)
        self._last_wake = 0.0

    def _on_confident(self) -> None:
        """Wake word clearly heard: chime, and make a talking Jarvis stop at once so the
        user isn't talked over while giving the command."""
        play_chime()
        threading.Thread(target=send_to_gui, args=({"action": "interrupt"},),
                         daemon=True).start()

    def _on_clip(self, clip: WakeClip) -> None:
        # Don't block the audio loop: verification/transcription takes a moment.
        threading.Thread(target=self._handle_clip, args=(clip,), daemon=True).start()

    def _handle_clip(self, clip: WakeClip) -> None:
        log.info("trigger: peak %.2f confident=%s", clip.peak_score, clip.confident)
        if not clip.confident:
            if not self.verifier.heard_wake_word(clip.audio):
                return
            play_chime()
        if time.monotonic() - self._last_wake < 1.5:
            return  # duplicate trigger for the same utterance
        self._last_wake = time.monotonic()
        open_gui(save_clip(clip.audio))

    def run(self) -> None:
        backoff = 1.0
        while True:
            proc = subprocess.Popen(RECORD_CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            log.info("listening for the wake word")
            started = time.monotonic()
            try:
                self._pump(proc)
            finally:
                proc.kill()
                proc.wait()
            # The recorder ended (device change, PipeWire restart): retry with backoff.
            backoff = 1.0 if time.monotonic() - started > 30 else min(backoff * 2, 30)
            log.warning("recorder stopped; restarting in %.0fs", backoff)
            time.sleep(backoff)

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout
        flag = speaking_flag()
        chunks = 0
        while True:
            chunks += 1
            if chunks % 50 == 0:  # every ~4 s
                reap_children()
            raw = proc.stdout.read(CHUNK_SAMPLES * 2)
            if len(raw) < CHUNK_SAMPLES * 2:
                return
            self.detector.strict = flag.exists()  # Jarvis is talking: confident wakes only
            self.detector.feed(np.frombuffer(raw, dtype=np.int16))


def acquire_single_instance_lock():
    lock = open(runtime_dir() / "wake.lock", "w")  # noqa: SIM115 - held for process lifetime
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    return lock


def install() -> None:
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\nType=Application\nName=Jarvis wake word\n"
        "Comment=Listens for “Hey Jarvis” and opens the assistant\n"
        f"Exec={PYTHON} -m jarvis.daemon\nPath={PROJECT_DIR}\n"
        "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=5\nNoDisplay=true\n"
    )
    LAUNCHER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER_FILE.write_text(
        "[Desktop Entry]\nType=Application\nName=Jarvis\nComment=Local desktop assistant\n"
        f"Exec={PYTHON} -m jarvis.main\nPath={PROJECT_DIR}\nIcon=audio-input-microphone\n"
        "Terminal=false\nCategories=Utility;\n"
    )
    print(f"Autostart entry: {AUTOSTART_FILE}\nApp launcher:    {LAUNCHER_FILE}")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "ab") as out:
        subprocess.Popen([PYTHON, "-m", "jarvis.daemon"], cwd=PROJECT_DIR, stdout=out,
                         stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    print("Wake listener started. Say “Hey Jarvis”.")


def uninstall() -> None:
    for path in (AUTOSTART_FILE, LAUNCHER_FILE):
        path.unlink(missing_ok=True)
    subprocess.run(["pkill", "-f", f"{PYTHON} -m jarvis.daemon"], check=False)
    print("Removed autostart and launcher entries; stopped the wake listener.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis-daemon", description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--install", action="store_true", help="Start at login and now")
    group.add_argument("--uninstall", action="store_true", help="Remove autostart and stop")
    args = parser.parse_args(argv)
    if args.install:
        install()
        return 0
    if args.uninstall:
        uninstall()
        return 0

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    lock = acquire_single_instance_lock()
    if lock is None:
        print("Jarvis wake listener is already running.")
        return 0
    WakeService().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
