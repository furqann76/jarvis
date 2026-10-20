# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Jarvis is a local-only voice and text agent that operates the user's desktop. The original
design is [jarvis_technical_specification.md](jarvis_technical_specification.md). The spec
targets Ubuntu 24.04, but the dev machine runs Ubuntu 26.04, GNOME **Wayland**. Everything
the spec lists is implemented: files, apps, AT-SPI desktop control, browser, web search,
voice, and keyboard/mouse with vision. The user asked for all of it at once, so there is no
phase gating any more.

## Commands

```bash
scripts/setup_env.sh                       # .venv (python3.12), deps, Chromium, Piper voice, Ollama models
.venv/bin/python -m jarvis.main            # GUI
.venv/bin/python -m jarvis.main --cli ["one-shot prompt"]
.venv/bin/pytest tests/unit                # fast, no services
.venv/bin/pytest tests/unit/test_security.py::test_unauthorized_directory_traversal
.venv/bin/pytest tests/integration         # real Ollama model + headless Chromium; auto-skip if missing
.venv/bin/ruff check src tests
```

- **Python:** the system Python is 3.14, so always use `.venv` (3.12).
- **Headless GUI:** run with `QT_QPA_PLATFORM=offscreen`.
- **Safe test runs:** point `JARVIS_CONFIG` at a TOML file whose `allowed_paths`,
  `database_path` and `trash_dir` live in a temp dir.
- **Live runs affect the user's desktop:** desktop, keyboard and mouse runs act on the real
  screen and apps. Say so before running one.

## Architecture

- **Request flow** (`agent/orchestrator.py`):
  1. `Planner` sends the conversation and the tool schemas to Ollama.
  2. Each proposed call goes through `security/validation.py`: JSON Schema, then
     `path_arguments` against the filesystem sandbox, then `url_arguments` against the URL
     policy.
  3. `tool.assess(args)` returns a per-call risk, which can only raise the class tier.
     - Raising `ValidationError` means the target doesn't exist, and the error goes back to
       the model.
     - Any other exception fails closed and requires approval.
  4. A `HIGH` risk call needs the injected `approver`.
  5. `execute`, then `verify`.
  6. When the model replies without tool calls, `Planner.check_answer` re-checks the answer
     against the tool results (`agent.check_answers`).
- **Failure handling:** errors are fed back to the model. The task FAILS after
  `max_failures` failed or unverified calls, or after `max_iterations` rounds. Denying an
  approval CANCELS the whole task.
- **State machine** (`agent/task_state.py`): every `Task.status` change goes through
  `transition`. After changing a step, call `sm.notify()`, which persists the task and
  pushes a deep-copied snapshot to `on_task_update`.
- **Tool groups** (`tools/`), all wired in `main.build_runtime`:
  - `files.py`: sandboxed filesystem.
  - `applications.py`: `.desktop` index, launched with `gio launch`. stderr goes to a temp
    file, never a pipe, because launched apps inherit it.
  - `web.py`: Brave HTML first, then Bing. Plain-HTTP Bing returns junk for non-browser
    clients.
  - `browser.py`: a Playwright persistent, headed context for tasks, plus a separate
    headless context for background page reads. Element refs are `data-jarvis-ref`
    attributes. A route guard enforces the URL policy on navigations.
  - `atspi.py` + `desktop.py`: AT-SPI over raw D-Bus via `dbus-fast`. There is no
    PyGObject, because the system `gi` is built for Python 3.14. The model sees short refs
    (`d12`), and `DesktopContext` maps them to D-Bus paths. Elements can be targeted by
    `application` plus label, and `find_element` normalises `*`→`×` and similar.
  - `portal.py` + `input.py`: keyboard and mouse through the xdg-desktop-portal
    RemoteDesktop API. Wayland blocks xdotool and PyAutoGUI. The restore token lives in
    `~/.local/share/jarvis/portal_restore_token`. Screen frames come from the ScreenCast
    stream through `gst-launch-1.0 pipewiresrc`, because the Screenshot portal refuses
    non-interactive captures. `models/vision.py` (Qwen2.5-VL) turns a target description
    into pixel coordinates.
- **Focusing other windows** (`InputContext.focus`), needed before any keystroke:
  1. AT-SPI `grab_focus`, which rarely works on Wayland.
  2. GNOME overview: Super, type the launcher name from `ApplicationIndex.display_name`,
     Enter. This works for windows hidden behind others.
  3. A vision click on the title bar.
  Always poll for activation (`_wait_active`): the overview animation made a single fixed
  sleep report failure even though focus did arrive. `close_window` closes Jarvis-browser
  tabs directly, and other windows with focus plus Ctrl+W (browsers) or Alt+F4.
- **No fake "done":** if a tool-less answer claims an action (`claims_action`), it still
  goes through `check_answer`. This happened with "Close YouTube" → "Done." when no tool
  existed.
- **Safety invariants. Keep these when changing tools:**
  - Desktop tools must refuse Jarvis's own windows (`DesktopContext.check_target`, by PID).
    Otherwise the model could click Approve on its own requests.
  - Keyboard tools require `application`, and `InputContext.focus` must confirm that app is
    focused before sending keys. Typing into whatever is focused once sent keys into the
    user's Chrome tab.
  - Protected apps (`permissions.desktop.protected_applications`, e.g. terminals) make any
    input HIGH risk.
- **Dashboard UI** (`ui/dashboard/`, used by `ui/main_window.py`). The window is frameless
  with a custom header, and the header has the Microphone and Voice toggles.
  - `theme.py`: palette and stylesheet.
  - `icons.py`: inline SVG line icons.
  - `widgets.py`: cards, the animated Orb and waveforms, `FlowLayout`, and
    `ElidedLabel`. Use `ElidedLabel` for any text that can be long (paths), because a
    plain QLabel widens the centre column and pushes content off-screen.
  - `home.py`: the hero, `CommandBar`, and `TimelineCard`. `timeline_rows` and
    `humanize_step` are pure functions and are tested.
  - `right_panel.py`: Activity, inline Approvals (these replaced the modal dialog),
    Recent Tasks, and System Info.
  - `pages.py`: the sidebar pages.
  - Activity states (idle/listening/thinking/speaking) drive the orb, waveforms and the
    Activity card through `MainWindow._set_activity`.
  - Check visual changes with an offscreen `win.grab()` screenshot at 1366 px width.
- **UI threading:**
  - `AsyncRunner` runs the orchestrator, STT and TTS on one asyncio thread.
  - `AgentBridge` Qt signals are emitted from worker threads and delivered queued to the Qt
    thread.
  - Approval and the minimize-before-input step (`yield_focus`) pass a
    `concurrent.futures.Future` through a signal.
  - Never touch widgets from worker threads.
- **Wake service** (`daemon.py`, `voice/wakeword.py`, `ipc.py`):
  - It's an always-on process started by XDG autostart. openWakeWord's `hey_jarvis` model
    runs on the CPU.
  - A score of 0.5 or more opens Jarvis immediately with a chime. A score between 0.15 and
    0.5 is first confirmed by CPU Whisper `base.en` plus `parse_wake`. Quick "Jarvis, do X"
    phrasing scores low.
  - The clip, including the command, is saved as a WAV in `$XDG_RUNTIME_DIR/jarvis/`.
  - The clip is sent to the GUI over the Unix socket `gui.sock`, where the window runs a
    `QLocalServer`. If no GUI is running, the daemon spawns `jarvis.main --wake-audio`.
  - The GUI is single-instance: a second launch forwards to the first.
  - The GUI touches `speaking` while TTS plays. During that time the detector runs in
    `strict` mode (confident wakes only), and `to_speakable` never says "Jarvis". A
    confident wake sends `{"action": "interrupt"}` at once, so TTS stops before the user
    gives the command (barge-in). `TextToSpeech.stop` bumps a generation counter, so an
    interrupted synthesis never starts playing.
  - Bringing the window forward (`ui/window_activation.py`): GNOME on Wayland ignores
    background raise requests from native Wayland windows, which opened the window behind
    the user's work. So on Wayland the GUI runs on Qt's **xcb** platform (XWayland), and
    `bring_to_front` sends `_NET_ACTIVE_WINDOW` with source=pager, which mutter honours.
    Qt's xcb plugin needs `libxcb-cursor0`; if the system lacks it, `setup_env.sh` unpacks
    it under `~/.local/share/jarvis/xcb-libs` and `configure_platform` preloads it. Turn
    this off with `[ui] prefer_x11 = false`.
  - Testing focus: Qt windows don't show up in AT-SPI here (`org.a11y.Status IsEnabled` is
    false), so check the X11 `_NET_ACTIVE_WINDOW` and `WM_STATE` instead.
  - Noise floor: use a percentile over the last few seconds. EMA-on-quiet-chunks never
    converges in a room louder than the initial guess, which was a real bug here.
- **Voice** (`voice/`):
  - `pw-record` and `pw-play` handle audio (PipeWire; there is no PortAudio).
  - `stt.py` preloads cuBLAS/cuDNN from the `nvidia-*` wheels, then falls back to CPU. Pass
    numpy audio, not files: PyAV's `decode_audio` is broken with the installed `av`.
  - In hands-free mode, `SpeechSegmenter` calibrates the noise floor first (the user's room
    sits around RMS 0.025), then `wake.parse_wake` checks for the wake word.
  - The listener is paused while TTS speaks.
- **Models:**
  - Default planner: `qwen3:8b` with `think: true`. qwen2.5-7b and qwen3 without thinking
    pressed the wrong buttons and invented results in desktop tests.
  - Ollama context: `num_ctx` is 16384, because the default is too small for 29 tool
    schemas.
  - `parse_message` strips `<think>` blocks and falls back to `<tool_call>` JSON written in
    the message text.

## Decisions on spec inconsistencies

- The tool is named `list_directory`. `tool_call_id` is the first positional parameter of
  `execute`.
- An approved call goes AWAITING_APPROVAL → EXECUTING → VERIFYING.
- `TaskStep` and `Task` carry `risk_level`, `final_response` and timestamps beyond the spec.
- The spec's PyAutoGUI/xlib input fallback is replaced by the RemoteDesktop portal, because
  X11-only tools don't work on Wayland.
