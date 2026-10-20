"""Deterministic task state machine. All Task.status changes go through here."""

from collections.abc import Callable

from jarvis.models.schemas import Task, TaskStatus, utcnow

S = TaskStatus

_CANCEL_OR_FAIL = {S.FAILED, S.CANCELLED}

ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    S.RECEIVED: {S.PLANNING} | _CANCEL_OR_FAIL,
    # PLANNING -> COMPLETED when the LLM answers without requesting further tools.
    S.PLANNING: {S.AWAITING_APPROVAL, S.EXECUTING, S.COMPLETED} | _CANCEL_OR_FAIL,
    # Approved calls go through EXECUTING (the spec diagram's direct arrow to VERIFYING
    # would skip execution).
    S.AWAITING_APPROVAL: {S.EXECUTING} | _CANCEL_OR_FAIL,
    S.EXECUTING: {S.VERIFYING} | _CANCEL_OR_FAIL,
    # After verification: back to PLANNING (next round / safe retry), or straight on to
    # the next tool call from the same LLM response.
    S.VERIFYING: {S.PLANNING, S.EXECUTING, S.AWAITING_APPROVAL} | _CANCEL_OR_FAIL,
    S.COMPLETED: set(),
    S.FAILED: set(),
    S.CANCELLED: set(),
}


class InvalidTransition(Exception):
    pass


class TaskStateMachine:
    def __init__(self, task: Task, on_change: Callable[[Task], None] | None = None):
        self.task = task
        self._on_change = on_change

    @property
    def status(self) -> TaskStatus:
        return self.task.status

    def can_transition(self, target: TaskStatus) -> bool:
        return target in ALLOWED_TRANSITIONS[self.task.status]

    def transition(self, target: TaskStatus, error: str | None = None) -> None:
        if not self.can_transition(target):
            raise InvalidTransition(f"{self.task.status.value} -> {target.value} is not allowed")
        self.task.status = target
        if error is not None:
            self.task.error_message = error
        self.notify()

    def notify(self) -> None:
        """Persist/broadcast the task after any change (status or step contents)."""
        self.task.updated_at = utcnow()
        if self._on_change:
            self._on_change(self.task)
