from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


class RiskLevel(StrEnum):
    SAFE = "SAFE"  # Tier 0: read-only, no permission required
    LOW = "LOW"  # Tier 1: reversible mutation, notify in UI
    HIGH = "HIGH"  # Tier 2: non-reversible, explicit interactive approval required

    @property
    def rank(self) -> int:
        return _RISK_RANK[self]


_RISK_RANK = {RiskLevel.SAFE: 0, RiskLevel.LOW: 1, RiskLevel.HIGH: 2}


def max_risk(*levels: RiskLevel) -> RiskLevel:
    return max(levels, key=lambda r: r.rank)


class ToolCallRequest(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool_call_id: str
    success: bool
    data: Any | None = None
    error: str | None = None
    verification_passed: bool = False


class TaskStep(BaseModel):
    step_id: int
    description: str
    tool_call: ToolCallRequest | None = None
    risk_level: RiskLevel | None = None
    status: TaskStatus = TaskStatus.RECEIVED
    result: ToolResult | None = None


class Task(BaseModel):
    id: str
    user_prompt: str
    status: TaskStatus = TaskStatus.RECEIVED
    steps: list[TaskStep] = Field(default_factory=list)
    final_response: str | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
