"""Wake-word parsing on transcripts ("Hey Jarvis, open Firefox" -> "open Firefox")."""

import re

# Whisper's common spellings of "Jarvis".
_WAKE = re.compile(
    r"^\W*(?:(?:hey|hi|okay|ok|hello|yo)\W+)?(?:jarvis|jervis|javis|jarvas|jarviss|charvis)\b"
    r"[\s,.!?:;-]*(?P<command>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_STOP = re.compile(r"^\W*(stop|cancel|never ?mind|be quiet|shut up)\W*$", re.IGNORECASE)


def parse_wake(transcript: str) -> tuple[bool, str]:
    """(woke, command). command may be empty when only the wake word was said."""
    match = _WAKE.match(transcript.strip())
    if not match:
        return False, ""
    return True, match.group("command").strip().rstrip(".").strip()


def is_stop_command(command: str) -> bool:
    return bool(_STOP.match(command))
