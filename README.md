# Jarvis Desktop

A local-first voice and text assistant that operates your Ubuntu desktop. It opens apps,
searches the web, works in websites and app windows, uses the keyboard and mouse, and talks
back. Everything runs on local models through Ollama, with no paid APIs. The design is in
[jarvis_technical_specification.md](jarvis_technical_specification.md).

Tested on Ubuntu 26.04 (GNOME, Wayland) with an RTX 4060 Ti 16 GB.

## Setup

```bash
scripts/setup_env.sh      # venv (Python 3.12), deps incl. CUDA libs, Chromium, Piper voice, Ollama models
.venv/bin/python -m jarvis.main
```

The first time Jarvis uses the keyboard or mouse, GNOME shows a **Remote desktop** dialog.
Allow it and share your main screen. Jarvis remembers this until you revoke it in
Settings → Privacy.

## Wake word ("Hey Jarvis")

`scripts/setup_env.sh` installs a small background listener that starts at login
(`~/.config/autostart/jarvis-wake.desktop`).

- **Waking it:** say **"Hey Jarvis"** or **"Jarvis"** at any time, even with the Jarvis
  window closed or minimized. You hear a chime and the window comes to the front, over
  whatever you're using.
- **Commands:** say the command in the same breath ("Hey Jarvis, open the calculator") or
  after the chime.
- **Privacy:** audio is scored locally by a small wake-word model and discarded. Nothing is
  transcribed until the wake word is heard.
- **Cost:** about 8% of one CPU core and roughly 230 MB of RAM.
- **Managing it:**
  - `.venv/bin/python -m jarvis.daemon --install` sets it up again.
  - `.venv/bin/python -m jarvis.daemon --uninstall` removes it.
  - The log is at `~/.local/state/jarvis/wake.log`.

## Using it

- **Type** a request, or click **🎤 Talk** (or press Ctrl+Space), speak, and click again.
- **Hands-free:** tick *Hands-free* and say "Jarvis, …" (e.g. "Jarvis, open the
  calculator"). Saying "Jarvis" alone gets a "Yes?" and waits for your command. "Jarvis,
  stop" cancels the current task.
- **Speak replies** reads answers aloud.
- **Terminal mode:** `python -m jarvis.main --cli`, or a one-shot
  `python -m jarvis.main "open youtube"`.

## What it can do

| Area | Tools |
| :--- | :--- |
| Apps | `list_applications`, `launch_application` (any installed .desktop app), `open_file` |
| Web | `web_search` (Brave, Bing as fallback; also reads the top pages), `fetch_webpage`, `open_website` (your browser) |
| Browser automation | `browser_open`, `browser_read`, `browser_click`, `browser_type`, `submit_web_form` (own Chromium profile) |
| App windows (accessibility) | `list_windows`, `get_active_window`, `inspect_window`, `activate_element` (by label, or a sequence of labels), `set_element_text` |
| Keyboard & mouse | `type_keyboard`, `press_keys`, `click_on_screen` (vision finds the target), `mouse_click`, `scroll`, `look_at_screen` |
| Files | list, search, read, create folder, trash, move, permanent delete (inside allowed folders only) |

## Safety

- **Approval:** high-impact actions show an approval dialog, with **Deny** as the default.
  These include:
  - permanent deletes and file moves
  - submitting POST or login forms
  - controls labelled Send, Buy, Delete, Confirm and similar
  - any typing or clicking in Terminal, VS Code or Settings
- **Keyboard input:** keystrokes are only sent once the named target app has focus. Jarvis
  refuses rather than typing into the wrong window.
- **Jarvis's own windows:** Jarvis can't click or type into them, so it can never approve
  its own requests.
- **Files:** file tools only work inside `permissions.filesystem.allowed_paths`.
- **Web addresses:** only http(s) URLs can be opened, never local or LAN addresses, and
  you can block domains.
- **Untrusted content:** web pages, files and screen text are treated as data, not
  instructions.
- **Answer check:** before an answer is shown, it's checked against what the tools
  actually returned.
- **Audit log:** every decision is recorded in `~/.local/share/jarvis/jarvis.db`.

## Models

| Role | Default | Notes |
| :--- | :--- | :--- |
| Planner | `qwen3:8b` (thinking on) | The most reliable at multi-step tasks in testing. For speed, set `think = false` or use `qwen2.5:7b-instruct-q4_K_M` (much faster, more mistakes). |
| Vision | `qwen2.5vl:7b` | Reads the screen for `look_at_screen` and `click_on_screen`. |
| Speech | faster-whisper `small.en` (GPU), Piper `en_US-lessac-medium` | |

## Tests

```bash
.venv/bin/pytest tests/unit          # fast, no services
.venv/bin/pytest tests/integration   # real Ollama model + headless Chromium
.venv/bin/ruff check src tests
```
