"""Settings loading: built-in defaults overridden by a TOML file.

Lookup order for the TOML file: explicit path argument, the JARVIS_CONFIG environment
variable, ~/.config/jarvis/settings.toml, then the repository's config/settings.toml.
"""

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "settings.toml"
USER_CONFIG = Path("~/.config/jarvis/settings.toml").expanduser()


class OllamaSettings(BaseModel):
    host: str = "http://localhost:11434"
    model: str = "qwen3:8b"
    temperature: float = 0.2
    request_timeout_s: float = 300.0
    think: bool | None = True  # reasoning models only; None = don't send the flag
    num_ctx: int = 16384  # Ollama's small default context is too short for tool schemas + DOM


class AgentSettings(BaseModel):
    max_iterations: int = 12
    max_failures: int = 3
    # Re-check the final answer against the tool results before showing it (catches
    # invented details; costs one extra model call on tasks that used tools).
    check_answers: bool = True


class StorageSettings(BaseModel):
    database_path: str = "~/.local/share/jarvis/jarvis.db"

    @property
    def resolved_database_path(self) -> Path:
        return Path(self.database_path).expanduser()


class FilesystemPermissions(BaseModel):
    allowed_paths: list[str] = Field(
        default_factory=lambda: ["~/Downloads", "~/Documents", "~/Desktop", "/tmp/jarvis_workspace"]
    )
    deny_patterns: list[str] = Field(
        default_factory=lambda: ["~/.ssh/*", "~/.bashrc", "/etc/*", "/usr/*"]
    )
    trash_dir: str = "~/.local/share/Trash"


class BrowserPermissions(BaseModel):
    allowed_schemes: list[str] = Field(default_factory=lambda: ["http", "https"])
    blocked_domains: list[str] = Field(default_factory=list)
    allow_private_network: bool = False  # localhost / LAN addresses (router pages, etc.)


class DesktopPermissions(BaseModel):
    # Any interaction with these apps (typing, clicking) needs approval: typing into a
    # terminal is equivalent to running arbitrary commands. Matched against the AT-SPI
    # application name, case-insensitively.
    protected_applications: list[str] = Field(
        default_factory=lambda: [
            "org.gnome.Terminal", "gnome-terminal-server", "kgx", "ptyxis", "konsole",
            "xterm", "code", "gnome-control-center", "org.gnome.Settings",
        ]
    )


class ApplicationPermissions(BaseModel):
    # Desktop-file IDs or names that launch_application refuses outright.
    blocked_applications: list[str] = Field(default_factory=list)


class PermissionSettings(BaseModel):
    filesystem: FilesystemPermissions = Field(default_factory=FilesystemPermissions)
    browser: BrowserPermissions = Field(default_factory=BrowserPermissions)
    desktop: DesktopPermissions = Field(default_factory=DesktopPermissions)
    applications: ApplicationPermissions = Field(default_factory=ApplicationPermissions)


class VoiceSettings(BaseModel):
    enabled: bool = True
    stt_model: str = "small.en"  # faster-whisper model name (small.en / medium.en)
    stt_device: str = "cuda"  # "cuda" or "cpu"; falls back to CPU if CUDA fails to load
    stt_compute_type: str = "float16"
    language: str = "en"
    max_record_seconds: float = 30.0
    tts_engine: str = "kokoro"  # "kokoro" (refined British "Jarvis-style" voices) or "piper"
    tts_voice: str = "~/.local/share/jarvis/voices/en_US-lessac-medium.onnx"  # piper
    kokoro_model_dir: str = "~/.local/share/jarvis/voices/kokoro"
    # Closest to J.A.R.V.I.S. in testing (~117 Hz, calm British): Daniel's crisp delivery
    # blended with Lewis's depth.
    kokoro_voice: str = "bm_daniel:0.6,bm_lewis:0.4"
    kokoro_speed: float = 0.9
    speak_responses: bool = True
    max_spoken_chars: int = 600


class BrowserSettings(BaseModel):
    headless: bool = False
    profile_dir: str = "~/.local/share/jarvis/browser-profile"
    navigation_timeout_ms: int = 30000


class DesktopSettings(BaseModel):
    vision_model: str = "qwen2.5vl:7b"  # Ollama model for look_at_screen / click_on_screen
    portal_token_file: str = "~/.local/share/jarvis/portal_restore_token"


class UiSettings(BaseModel):
    # On Wayland, run the window via XWayland so the wake word can bring it to the front
    # (GNOME blocks background raises of native Wayland windows).
    prefer_x11: bool = True


class Settings(BaseModel):
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    desktop: DesktopSettings = Field(default_factory=DesktopSettings)
    ui: UiSettings = Field(default_factory=UiSettings)


def find_config_file(explicit: str | os.PathLike | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("JARVIS_CONFIG")
    if env:
        return Path(env).expanduser()
    for candidate in (USER_CONFIG, REPO_CONFIG):
        if candidate.is_file():
            return candidate
    return None


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    config_file = find_config_file(path)
    if config_file is None:
        return Settings()
    with open(config_file, "rb") as fh:
        return Settings.model_validate(tomllib.load(fh))
