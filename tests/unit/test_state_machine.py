import pytest

from jarvis.agent.task_state import InvalidTransition, TaskStateMachine
from jarvis.models.schemas import Task, TaskStatus

S = TaskStatus


def machine() -> tuple[TaskStateMachine, list[TaskStatus]]:
    seen: list[TaskStatus] = []
    sm = TaskStateMachine(Task(id="t", user_prompt="p"), on_change=lambda t: seen.append(t.status))
    return sm, seen


def test_happy_path_with_approval():
    sm, seen = machine()
    for status in (S.PLANNING, S.AWAITING_APPROVAL, S.EXECUTING, S.VERIFYING, S.PLANNING,
                   S.COMPLETED):
        sm.transition(status)
    assert seen == [S.PLANNING, S.AWAITING_APPROVAL, S.EXECUTING, S.VERIFYING, S.PLANNING,
                    S.COMPLETED]


def test_multiple_tool_calls_in_one_round():
    sm, _ = machine()
    for status in (S.PLANNING, S.EXECUTING, S.VERIFYING, S.AWAITING_APPROVAL, S.EXECUTING,
                   S.VERIFYING, S.EXECUTING, S.VERIFYING, S.PLANNING):
        sm.transition(status)
    assert sm.status == S.PLANNING


@pytest.mark.parametrize(
    "path",
    [
        (S.EXECUTING,),  # cannot skip planning
        (S.PLANNING, S.VERIFYING),  # cannot verify without executing
        (S.PLANNING, S.AWAITING_APPROVAL, S.VERIFYING),  # approval must lead to execution
        (S.PLANNING, S.EXECUTING, S.COMPLETED),  # completion only after the LLM concludes
    ],
)
def test_invalid_transitions(path):
    sm, _ = machine()
    with pytest.raises(InvalidTransition):
        for status in path:
            sm.transition(status)


@pytest.mark.parametrize("terminal", [S.COMPLETED, S.FAILED, S.CANCELLED])
def test_terminal_states_are_final(terminal):
    sm, _ = machine()
    sm.transition(S.PLANNING)
    sm.transition(terminal)
    assert terminal.is_terminal
    for target in S:
        assert not sm.can_transition(target)


def test_every_active_state_can_fail_or_cancel():
    for start in (S.RECEIVED, S.PLANNING, S.AWAITING_APPROVAL, S.EXECUTING, S.VERIFYING):
        for end in (S.FAILED, S.CANCELLED):
            sm = TaskStateMachine(Task(id="t", user_prompt="p", status=start))
            sm.transition(end, error="x")
            assert sm.task.error_message == "x"
