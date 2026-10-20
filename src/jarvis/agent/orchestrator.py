"""Agent loop: plan with the LLM, then validate -> (approve) -> execute -> verify each tool call.

The LLM only ever *proposes* tool calls. Nothing touches the system unless it passes
security.validation, and HIGH-risk calls additionally require the injected approver.
"""

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable

from jarvis.agent.planner import Planner, assistant_tool_call_message, tool_result_message
from jarvis.agent.task_state import TaskStateMachine
from jarvis.agent.tool_registry import ToolRegistry
from jarvis.config import AgentSettings
from jarvis.models.ollama_client import OllamaError
from jarvis.models.schemas import (
    RiskLevel,
    Task,
    TaskStatus,
    TaskStep,
    ToolCallRequest,
    ToolResult,
    max_risk,
)
from jarvis.security.audit import AuditEvent, AuditLogger
from jarvis.security.permissions import SecurityPolicy
from jarvis.security.validation import ValidationError, validate_tool_call
from jarvis.storage.repository import TaskRepository
from jarvis.tools.base import BaseTool

log = logging.getLogger(__name__)

# Called for HIGH-risk tool calls. Return True to allow execution.
Approver = Callable[[Task, ToolCallRequest, BaseTool], Awaitable[bool]]
TaskListener = Callable[[Task], None]


async def deny_all(task: Task, call: ToolCallRequest, tool: BaseTool) -> bool:
    return False


_ACTION_CLAIM = re.compile(
    r"\b(done|opened|closed|launched|started|stopped|deleted|removed|moved|created|sent|"
    r"saved|played|playing|typed|clicked|installed|turned (on|off)|switched)\b",
    re.IGNORECASE,
)


def claims_action(answer: str) -> bool:
    return bool(_ACTION_CLAIM.search(answer))


class UserDenied(Exception):
    pass


class Orchestrator:
    def __init__(
        self,
        planner: Planner,
        registry: ToolRegistry,
        policy: SecurityPolicy,
        audit: AuditLogger,
        settings: AgentSettings,
        approver: Approver = deny_all,
        task_repository: TaskRepository | None = None,
        on_task_update: TaskListener | None = None,
    ):
        self.planner = planner
        self.registry = registry
        self.policy = policy
        self.audit = audit
        self.settings = settings
        self.approver = approver
        self.task_repository = task_repository
        self.on_task_update = on_task_update

    def _publish(self, task: Task) -> None:
        if self.task_repository:
            self.task_repository.save(task)
        if self.on_task_update:
            # Listeners may live on another thread (Qt UI); hand them a snapshot.
            self.on_task_update(task.model_copy(deep=True))

    async def run(self, user_prompt: str) -> Task:
        task = Task(id=uuid.uuid4().hex, user_prompt=user_prompt)
        sm = TaskStateMachine(task, on_change=self._publish)
        sm.notify()
        self.audit.record(AuditEvent.TASK_RECEIVED, task.id, detail=user_prompt)
        try:
            await self._loop(task, sm)
        except UserDenied as e:
            task.final_response = str(e)
            sm.transition(TaskStatus.CANCELLED, error=str(e))
        except asyncio.CancelledError:
            if not task.status.is_terminal:
                task.final_response = "Task cancelled."
                sm.transition(TaskStatus.CANCELLED, error="Cancelled by user")
            raise
        except OllamaError as e:
            self._fail(sm, f"Language model error: {e}")
        except Exception as e:  # noqa: BLE001 - any bug must still end the task cleanly
            log.exception("Task %s crashed", task.id)
            self._fail(sm, f"Internal error: {e}")
        finally:
            self.audit.record(AuditEvent.TASK_FINISHED, task.id, detail=task.status.value)
        return task

    def _fail(self, sm: TaskStateMachine, message: str) -> None:
        if not sm.task.status.is_terminal:
            sm.task.final_response = message
            sm.transition(TaskStatus.FAILED, error=message)

    async def _loop(self, task: Task, sm: TaskStateMachine) -> None:
        messages = self.planner.start_conversation(task.user_prompt)
        tools = self.registry.ollama_schemas()
        failures = 0

        for _ in range(self.settings.max_iterations):
            if task.status != TaskStatus.PLANNING:  # stays PLANNING if all calls were rejected
                sm.transition(TaskStatus.PLANNING)
            response = await self.planner.next_step(messages, tools)

            if not response.tool_calls:
                answer = response.content or "Done."
                # Check answers built on tool results, and also tool-less answers that claim
                # an action ("Done.", "Closed it") which nothing actually performed.
                if self.settings.check_answers and (task.steps or claims_action(answer)):
                    try:
                        answer = await self.planner.check_answer(messages, answer)
                    except OllamaError as e:  # the unchecked answer is still useful
                        log.warning("Answer check failed: %s", e)
                task.final_response = answer
                sm.transition(TaskStatus.COMPLETED)
                self.planner.remember(task.user_prompt, task.final_response)
                return

            messages.append(assistant_tool_call_message(response.content, response.tool_calls))
            for call in response.tool_calls:
                result = await self._handle_call(task, sm, call)
                messages.append(tool_result_message(call, result))
                if not (result.success and result.verification_passed):
                    failures += 1
            if failures >= self.settings.max_failures:
                self._fail(sm, f"Stopped after {failures} failed tool calls.")
                return

        self._fail(sm, f"Gave up after {self.settings.max_iterations} planning rounds.")

    async def _handle_call(
        self, task: Task, sm: TaskStateMachine, call: ToolCallRequest
    ) -> ToolResult:
        tool = self.registry.get(call.name)
        step = TaskStep(
            step_id=len(task.steps) + 1,
            description=tool.describe_call(call.arguments) if tool else f"{call.name}(...)",
            tool_call=call,
            risk_level=tool.risk_level if tool else None,
            status=TaskStatus.PLANNING,
        )
        task.steps.append(step)
        sm.notify()

        # 1. Validation & sandbox (before the user is ever asked to approve).
        try:
            if tool is None:
                raise ValidationError(
                    f"Unknown tool '{call.name}'. Available: {', '.join(self.registry.names())}"
                )
            validate_tool_call(tool, call.arguments, self.policy)
        except ValidationError as e:
            self.audit.record(AuditEvent.TOOL_REJECTED, task.id, call,
                              tool.risk_level if tool else None, str(e))
            step.result = ToolResult(tool_call_id=call.id, success=False, error=str(e))
            step.status = TaskStatus.FAILED
            sm.notify()
            return step.result

        # 2. Per-call risk: tools may escalate (never lower) their tier for this target.
        try:
            assessed, context = await tool.assess(call.arguments)
        except ValidationError as e:  # the target doesn't exist: tell the model
            self.audit.record(AuditEvent.TOOL_REJECTED, task.id, call, tool.risk_level, str(e))
            step.result = ToolResult(tool_call_id=call.id, success=False, error=str(e))
            step.status = TaskStatus.FAILED
            sm.notify()
            return step.result
        except Exception as e:  # noqa: BLE001 - fail closed: unknown risk => approval
            log.warning("Risk assessment of %s failed: %s", tool.name, e)
            assessed, context = RiskLevel.HIGH, f"risk assessment failed: {e}"
        risk = max_risk(tool.risk_level, assessed)
        step.risk_level = risk
        if context:
            step.description = f"{step.description} [{context}]"
        sm.notify()

        # 3. Interactive approval for HIGH-risk calls.
        if self.policy.requires_approval(risk):
            sm.transition(TaskStatus.AWAITING_APPROVAL)
            step.status = TaskStatus.AWAITING_APPROVAL
            sm.notify()
            self.audit.record(AuditEvent.APPROVAL_REQUESTED, task.id, call, risk)
            if not await self.approver(task, call, tool):
                self.audit.record(AuditEvent.APPROVAL_DENIED, task.id, call, risk)
                step.status = TaskStatus.CANCELLED
                raise UserDenied(f"You declined: {step.description}")
            self.audit.record(AuditEvent.APPROVAL_GRANTED, task.id, call, risk)

        # 4. Execute.
        sm.transition(TaskStatus.EXECUTING)
        step.status = TaskStatus.EXECUTING
        sm.notify()
        try:
            result = await tool.execute(call.id, **call.arguments)
        except Exception as e:  # noqa: BLE001 - tool bugs are reported to the LLM
            log.exception("Tool %s raised", tool.name)
            result = ToolResult(
                tool_call_id=call.id, success=False, error=f"{type(e).__name__}: {e}"
            )
        self.audit.record(
            AuditEvent.TOOL_EXECUTED if result.success else AuditEvent.TOOL_FAILED,
            task.id, call, risk, result.error,
        )

        # 5. Verify post-conditions on the real system.
        sm.transition(TaskStatus.VERIFYING)
        step.status = TaskStatus.VERIFYING
        step.result = result
        sm.notify()
        if result.success:
            try:
                result.verification_passed = await tool.verify(result)
            except Exception:  # noqa: BLE001
                log.exception("Verification of %s raised", tool.name)
                result.verification_passed = False
            if not result.verification_passed:
                self.audit.record(AuditEvent.VERIFICATION_FAILED, task.id, call, risk)

        step.status = (
            TaskStatus.COMPLETED if result.success and result.verification_passed
            else TaskStatus.FAILED
        )
        sm.notify()
        return result
