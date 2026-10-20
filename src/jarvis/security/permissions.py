"""Filesystem sandbox and risk-tier policy."""

import fnmatch
import ipaddress
import os
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from jarvis.models.schemas import RiskLevel


def normalize_path(path: str | os.PathLike) -> Path:
    """Expand ``~`` and resolve ``..``/symlinks. Every policy check runs on this form."""
    return Path(os.path.expanduser(os.fspath(path))).resolve()


def _is_private_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


class SecurityPolicy:
    def __init__(
        self,
        allowed_paths: Iterable[str | os.PathLike],
        deny_patterns: Iterable[str] = (),
        allowed_url_schemes: Iterable[str] = ("http", "https"),
        blocked_domains: Iterable[str] = (),
        allow_private_network: bool = False,
    ):
        self.allow_private_network = allow_private_network
        self.allowed_url_schemes = {s.lower() for s in allowed_url_schemes}
        self.blocked_domains = [d.lower().lstrip(".") for d in blocked_domains]
        self.allowed_roots: list[Path] = [normalize_path(p) for p in allowed_paths]
        # Deny patterns are expanded but deliberately not resolved, so a pattern such as
        # "/etc/*" keeps its glob characters. Both the raw and the resolved form are kept
        # so that symlinked system dirs (e.g. /etc -> /private/etc) are still matched.
        self.deny_patterns: list[str] = []
        for pattern in deny_patterns:
            expanded = os.path.expanduser(pattern)
            self.deny_patterns.append(expanded)
            head, sep, tail = expanded.partition("*")
            if sep:
                resolved_head = str(Path(head).resolve()) if head else ""
                if head.endswith("/") and not resolved_head.endswith("/"):
                    resolved_head += "/"
                alt = resolved_head + sep + tail
            else:
                alt = str(Path(expanded).resolve())
            if alt not in self.deny_patterns:
                self.deny_patterns.append(alt)

    def is_denied(self, path: str | os.PathLike) -> bool:
        resolved = normalize_path(path)
        text = str(resolved)
        for pattern in self.deny_patterns:
            if fnmatch.fnmatchcase(text, pattern):
                return True
            # "dir/*" also denies the directory itself.
            if pattern.endswith("/*") and text == pattern[:-2].rstrip("/"):
                return True
        return False

    def is_path_allowed(self, path: str | os.PathLike) -> bool:
        resolved = normalize_path(path)
        if self.is_denied(resolved):
            return False
        return any(_is_within(resolved, root) for root in self.allowed_roots)

    def url_rejection_reason(self, url: str) -> str | None:
        """None if the URL may be opened, otherwise a human-readable reason."""
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            return f"Malformed URL: {url!r}"
        if parts.scheme.lower() not in self.allowed_url_schemes:
            allowed = ", ".join(sorted(self.allowed_url_schemes))
            return f"URL scheme '{parts.scheme}' is not allowed (allowed: {allowed})"
        if not parts.hostname:
            return f"URL has no host: {url!r}"
        if parts.username or parts.password:
            return "URLs with embedded credentials are not allowed"
        if self.is_domain_blocked(parts.hostname):
            return f"Domain is blocked by policy: {parts.hostname}"
        if not self.allow_private_network and _is_private_host(parts.hostname):
            return f"Local/private network addresses are not allowed: {parts.hostname}"
        return None

    def is_domain_blocked(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return any(host == d or host.endswith("." + d) for d in self.blocked_domains)

    def is_url_allowed(self, url: str) -> bool:
        return self.url_rejection_reason(url) is None

    @staticmethod
    def requires_approval(risk: RiskLevel) -> bool:
        return risk == RiskLevel.HIGH

    @staticmethod
    def requires_notification(risk: RiskLevel) -> bool:
        return risk in (RiskLevel.LOW, RiskLevel.HIGH)
