import json
import socket
import threading

import numpy as np
import pytest

from jarvis import ipc
from jarvis.voice.microphone import load_wav
from jarvis.voice.wakeword import CHUNK_SAMPLES, WakeClip, WakeDetector


def chunks(level: float, count: int, seed: int = 0) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    return [(rng.normal(0, level, CHUNK_SAMPLES) * 32768).clip(-32768, 32767).astype(np.int16)
            for _ in range(count)]


class Script:
    """Scorer that returns a preset score per chunk."""

    def __init__(self, scores):
        self.scores = list(scores)

    def __call__(self, chunk):
        return self.scores.pop(0) if self.scores else 0.0


def run(detector: WakeDetector, segments):
    for level, count in segments:
        for c in chunks(level, count):
            detector.feed(c)


def test_confident_wake_with_command_in_one_breath():
    clips: list[WakeClip] = []
    chimes = []
    # 20 quiet chunks, then 5 chunks of "hey jarvis" (high score), 15 of command speech,
    # then silence until the detector closes the clip.
    scorer = Script([0.0] * 20 + [0.9] * 5 + [0.0] * 200)
    det = WakeDetector(scorer, clips.append, lambda: chimes.append(1))
    run(det, [(0.002, 20), (0.2, 5), (0.2, 15), (0.002, 20)])
    assert len(clips) == 1 and chimes == [1]
    clip = clips[0]
    assert clip.confident and clip.peak_score == pytest.approx(0.9)
    # pre-roll (2 s = 25 chunks max) + wake + command + ~0.9 s silence
    assert 30 * CHUNK_SAMPLES <= clip.audio.size <= 60 * CHUNK_SAMPLES


def test_bare_wake_word_waits_for_command_then_gives_up():
    clips: list[WakeClip] = []
    scorer = Script([0.0] * 10 + [0.95] * 4)
    det = WakeDetector(scorer, clips.append, wait_for_command_s=1.0)
    run(det, [(0.002, 10), (0.2, 4), (0.002, 30)])
    assert len(clips) == 1 and clips[0].confident


def test_low_score_candidate_is_marked_unconfident_and_no_chime():
    clips: list[WakeClip] = []
    chimes = []
    scorer = Script([0.0] * 10 + [0.2] * 3)
    det = WakeDetector(scorer, clips.append, lambda: chimes.append(1))
    run(det, [(0.002, 10), (0.2, 3), (0.2, 10), (0.002, 20)])
    assert len(clips) == 1 and not clips[0].confident and chimes == []


def test_ignores_audio_while_paused_and_below_threshold():
    clips: list[WakeClip] = []
    det = WakeDetector(Script([0.9] * 50), clips.append)
    det.paused = True
    run(det, [(0.2, 50), (0.002, 30)])
    assert clips == []
    det.paused = False
    det.scorer = Script([0.05] * 100)  # never reaches the low threshold
    run(det, [(0.2, 50), (0.002, 30)])
    assert clips == []


def test_clip_length_is_capped():
    clips: list[WakeClip] = []
    det = WakeDetector(Script([0.9] * 3), clips.append, max_clip_s=2.0)
    run(det, [(0.3, 100)])  # someone keeps talking
    assert clips and all(c.audio.size <= 2.1 * 16000 + 26 * CHUNK_SAMPLES for c in clips)


def test_ipc_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert ipc.send_to_gui({"action": "wake"}) is False  # nothing listening

    received = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(ipc.gui_socket_path()))
    server.listen(1)

    def accept():
        conn, _ = server.accept()
        with conn:
            received.append(json.loads(conn.makefile().readline()))

    t = threading.Thread(target=accept)
    t.start()
    assert ipc.send_to_gui({"action": "wake", "audio": "/x.wav"})
    t.join(2)
    server.close()
    assert received == [{"action": "wake", "audio": "/x.wav"}]


def test_wake_clip_wav_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    from jarvis.daemon import save_clip

    audio = np.linspace(-0.5, 0.5, 16000, dtype=np.float32)
    path = save_clip(audio)
    assert path.parent == ipc.runtime_dir()
    back = load_wav(path)
    assert back.shape == audio.shape and np.allclose(back, audio, atol=1e-3)


def test_clip_ends_in_a_noisy_room():
    """Background at RMS 0.025 (louder than the initial floor) must still end the clip."""
    clips: list[WakeClip] = []
    scorer = Script([0.0] * 40 + [0.9] * 5)
    det = WakeDetector(scorer, clips.append)
    run(det, [(0.025, 40), (0.2, 5), (0.2, 12), (0.025, 20)])
    assert len(clips) == 1


def test_strict_mode_while_speaking_needs_a_confident_wake():
    clips: list[WakeClip] = []
    det = WakeDetector(Script([0.0] * 10 + [0.3] * 5 + [0.0] * 40), clips.append)
    det.strict = True  # Jarvis is talking: a borderline score must not trigger
    run(det, [(0.002, 10), (0.2, 5), (0.002, 40)])
    assert clips == []
    det.scorer = Script([0.9] * 5)
    run(det, [(0.2, 5), (0.002, 60)])
    assert len(clips) == 1 and clips[0].confident
