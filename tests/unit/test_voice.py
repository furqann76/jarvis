import numpy as np
import pytest

from jarvis.voice.listener import FRAME_SAMPLES, SpeechSegmenter
from jarvis.voice.microphone import pcm16_to_float
from jarvis.voice.tts import to_speakable
from jarvis.voice.wake import is_stop_command, parse_wake


@pytest.mark.parametrize("text,expected", [
    ("Jarvis, open Firefox.", (True, "open Firefox")),
    ("Hey Jarvis what's the weather", (True, "what's the weather")),
    ("OK, Jervis. Play music!", (True, "Play music!")),
    ("Jarvis.", (True, "")),
    ("  jarvis  ", (True, "")),
    ("I told Jarvis already", (False, "")),
    ("Jarvisland is nice", (False, "")),
    ("open firefox", (False, "")),
])
def test_parse_wake(text, expected):
    assert parse_wake(text) == expected


def test_stop_command():
    assert is_stop_command("stop.") and is_stop_command("Never mind")
    assert not is_stop_command("stop the music in spotify")


def test_pcm16_to_float():
    raw = np.array([0, 16384, -32768], dtype=np.int16).tobytes()
    assert np.allclose(pcm16_to_float(raw), [0.0, 0.5, -1.0])


def test_to_speakable():
    text = "**Done!** Moved `a.pdf` to /home/user/Documents/reports/pdfs.\n- item one\n# Title"
    assert to_speakable(text, 600) == "Done! Moved a.pdf to pdfs. item one Title"
    long = "First sentence. " * 100
    out = to_speakable(long, 100)
    assert len(out) <= 100 and out.endswith(".")


def _frames(level: float, count: int) -> list[np.ndarray]:
    rng = np.random.default_rng(0)
    return [rng.normal(0, level, FRAME_SAMPLES).astype(np.float32) for _ in range(count)]


def test_segmenter_emits_one_utterance_per_speech_burst():
    got: list[np.ndarray] = []
    seg = SpeechSegmenter(got.append, calibration_ms=0)
    for f in _frames(0.002, 50) + _frames(0.2, 40) + _frames(0.002, 40):
        seg.feed(f)
    assert len(got) == 1
    # pre-roll + speech + trailing silence ≈ (10 + 40 + 26) frames
    assert 60 * FRAME_SAMPLES <= got[0].size <= 80 * FRAME_SAMPLES

    for f in _frames(0.2, 40) + _frames(0.002, 40):
        seg.feed(f)
    assert len(got) == 2


def test_segmenter_ignores_clicks_and_short_blips():
    got: list[np.ndarray] = []
    seg = SpeechSegmenter(got.append, calibration_ms=0)
    for f in _frames(0.002, 30) + _frames(0.3, 2) + _frames(0.002, 30):  # 60 ms click
        seg.feed(f)
    for f in _frames(0.3, 5) + _frames(0.002, 40):  # 150 ms blip: below min length
        seg.feed(f)
    assert got == []


def test_segmenter_caps_utterance_length():
    got: list[np.ndarray] = []
    seg = SpeechSegmenter(got.append, max_utterance_s=1.0, calibration_ms=0)
    for f in _frames(0.002, 20) + _frames(0.3, 60):
        seg.feed(f)
    # Continuous speech is split into capped chunks rather than buffered forever.
    assert len(got) >= 1
    assert all(u.size <= seg.max_frames * FRAME_SAMPLES for u in got)


def test_segmenter_calibrates_to_noisy_room():
    """Background at RMS ~0.025 (above the fixed floor) must not count as speech."""
    got: list[np.ndarray] = []
    seg = SpeechSegmenter(got.append)
    for f in _frames(0.025, 100):
        seg.feed(f)
    assert got == [] and not seg.in_speech
    for f in _frames(0.2, 40) + _frames(0.025, 40):
        seg.feed(f)
    assert len(got) == 1


def test_segmenter_adapts_to_new_steady_noise():
    got: list[np.ndarray] = []
    seg = SpeechSegmenter(got.append, calibration_ms=0, max_utterance_s=1.0)
    for f in _frames(0.002, 20) + _frames(0.05, 200):  # a fan switches on
        seg.feed(f)
    detections = len(got)
    for f in _frames(0.05, 200):
        seg.feed(f)
    assert len(got) == detections  # stopped treating the fan as speech


def test_to_speakable_never_says_the_wake_word():
    assert to_speakable("Jarvis here. Hey Jarvis, I opened it.", 600) == "here. I opened it."


def test_tts_stop_during_synthesis_prevents_playback(tmp_path):
    from jarvis.config import VoiceSettings
    from jarvis.voice.tts import TextToSpeech

    played = []
    tts = TextToSpeech(VoiceSettings(), player=["true"])

    def slow_synth(text, path):
        tts.stop()  # the user interrupts while audio is being generated
        path.write_bytes(b"RIFF")

    tts.synthesize_to_file = slow_synth
    import subprocess as sp
    real_popen = sp.Popen
    sp.Popen = lambda *a, **k: played.append(a) or real_popen(["true"])
    try:
        tts.speak("Hello there")
    finally:
        sp.Popen = real_popen
    assert played == []
