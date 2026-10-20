"""The GUI modules aren't exercised by other unit tests; at least make sure they load."""

import importlib

import pytest


@pytest.mark.parametrize("module", [
    "jarvis.main", "jarvis.daemon", "jarvis.ui.main_window", "jarvis.ui.window_activation",
    "jarvis.ui.dashboard.home", "jarvis.ui.dashboard.pages", "jarvis.ui.dashboard.chrome",
])
def test_module_imports(module):
    importlib.import_module(module)


def test_configure_platform_respects_explicit_choice(monkeypatch):
    from jarvis.ui.window_activation import configure_platform

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert configure_platform() == "offscreen"
    monkeypatch.delenv("QT_QPA_PLATFORM")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert configure_platform() is None


def test_timeline_rows_and_humanized_steps():
    from jarvis.models.schemas import Task, TaskStatus, TaskStep, ToolCallRequest
    from jarvis.ui.dashboard.home import humanize_step, timeline_rows

    assert humanize_step("search_files(directory_path='~/Downloads', pattern='*')") == \
        "~/Downloads · *"
    assert humanize_step("Move 2 item(s) into /x") == "Move 2 item(s) into /x"
    assert humanize_step("activate_element(application='Calculator') [activate button '7' in "
                         "calc]") == "Calculator — activate button '7' in calc"

    task = Task(id="t", user_prompt="open calc", status=TaskStatus.AWAITING_APPROVAL)
    task.steps.append(TaskStep(step_id=1, description="d", status=TaskStatus.AWAITING_APPROVAL,
                               tool_call=ToolCallRequest(id="c", name="bulk_move_files")))
    rows = timeline_rows(task)
    assert [r.status for r in rows] == ["done", "approval", "pending"]
    assert rows[1].title == "Waiting for approval"
    task.status = TaskStatus.COMPLETED
    task.steps[0].status = TaskStatus.COMPLETED
    assert [r.status for r in timeline_rows(task)] == ["done", "done", "done"]


def test_relative_time_and_greeting():
    from datetime import UTC, datetime, timedelta

    from jarvis.ui.dashboard.home import greeting
    from jarvis.ui.dashboard.right_panel import relative_time

    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert relative_time(now - timedelta(minutes=2), now) == "2 minutes ago"
    assert relative_time(now - timedelta(hours=1), now) == "1 hour ago"
    assert greeting(datetime(2026, 1, 1, 8)) == "Good morning!"
    assert greeting(datetime(2026, 1, 1, 20)) == "Good evening!"


def test_command_bar_send_button_and_enter_submit(monkeypatch):
    """Regression: clicking the arrow passed clicked()'s bool as the text and did nothing."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from jarvis.ui.dashboard.home import CommandBar

    app = QApplication.instance() or QApplication([])
    bar = CommandBar()
    sent: list[str] = []
    bar.submitted.connect(sent.append)

    bar.input.setText("organize my downloads")
    bar.send_button.click()
    bar.input.setText("open the calculator")
    bar.input.returnPressed.emit()
    bar.input.setText("   ")
    bar.send_button.click()  # blank input is ignored
    assert sent == ["organize my downloads", "open the calculator"]
    assert bar.input.text() == "   " and app is not None
