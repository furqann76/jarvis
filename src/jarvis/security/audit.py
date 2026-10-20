"""Audit trail of every tool-call decision. Writes go to SQLite via AuditRepository."""

import logging
from enum import StrEnum

from jarvis.models.schemas import RiskLevel, ToolCallRequest
from jarvis.storage.repository import AuditRepository

log = logging.getLogger(__name__)


class AuditEvent(StrEnum):
    TASK_RECEIVED = "TASK_RECEIVED"
    TOOL_REJECTED = "TOOL_REJECTED"  # failed validation / sandbox check
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    TOOL_EXECUTED = "TOOL_EXECUTED"
    TOOL_FAILED = "TOOL_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    TASK_FINISHED = "TASK_FINISHED"


class AuditLogger:
    def __init__(self, repository: AuditRepository):
        self.repository = repository

    def record(
        self,
        event: AuditEvent,
        task_id: str,
        call: ToolCallRequest | None = None,
        risk: RiskLevel | None = None,
        detail: str | None = None,
    ) -> None:
        log.info("audit %s task=%s tool=%s %s", event.value, task_id,
                 call.name if call else "-", detail or "")
        self.repository.append(
            event.value,
            task_id=task_id,
            tool_call_id=call.id if call else None,
            tool_name=call.name if call else None,
            risk_level=risk.value if risk else None,
            arguments=call.arguments if call else None,
            detail=detail,
        )
