"""Heuristics for escalating UI interactions (desktop or web) to Tier 2."""

import re

# Labels of controls whose activation is typically irreversible or outward-facing.
_DESTRUCTIVE = re.compile(
    r"\b(delete|remove|erase|discard|destroy|wipe|format|uninstall|reset|empty trash|"
    r"send|post|publish|submit|share|reply|tweet|"
    r"buy|purchase|pay|checkout|check out|order|place order|subscribe|donate|transfer|"
    r"confirm|approve|accept|agree|authorize|allow|grant|"
    r"sign out|log ?out|deactivate|close account|unsubscribe|shut ?down|restart|reboot)\b",
    re.IGNORECASE,
)


def destructive_keyword(label: str | None) -> str | None:
    """The matched keyword if a control label looks high-impact, else None."""
    if not label:
        return None
    match = _DESTRUCTIVE.search(label)
    return match.group(0).lower() if match else None
