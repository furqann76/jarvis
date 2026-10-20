import asyncio
from pathlib import Path
from typing import Any

import pytest

from jarvis.agent.orchestrator import Orchestrator
from jarvis.agent.planner import Planner
from jarvis.agent.tool_registry import ToolRegistry
from jarvis.config import AgentSettings
from jarvis.models.ollama_client import ChatResponse, OllamaError
from jarvis.models.schemas import RiskLevel, TaskStatus, ToolCallRequest, ToolResult
from jarvis.security.audit import AuditLogger
from jarvis.security.permissions import SecurityPolicy
from jarvis.storage.database import Database
from jarvis.storage.repository import AuditRepository, TaskRepository
from jarvis.tools.base import BaseTool
from jarvis.tools.files import build_file_tools

S = TaskStatus


class ScriptedModel:
    """Fake LLM returning pre-baked responses and recording what it was sent."""

    def __init__(self, *responses: ChatResponse | Exception):
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages, tools=None) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def call(name: str, **arguments) -> ChatResponse:
    return ChatResponse(tool_calls=[ToolCallRequest(id=f"id_{name}", name=name,
                                                    arguments=arguments)])


def answer(text: str) -> ChatResponse:
    return ChatResponse(content=text)


class Harness:
    def __init__(self, model, policy: SecurityPolicy, db: Database, trash: Path,
                 approve: bool = True, **agent_settings):
        self.model = model
        self.statuses: list[TaskStatus] = []
        self.approvals: list[str] = []

        async def approver(task, call_req, tool):
            self.approvals.append(call_req.name)
            return approve

        self.tasks = TaskRepository(db)
        self.audit = AuditRepository(db)
        self.orchestrator = Orchestrator(
            planner=Planner(model, ["/sandbox"]),
            registry=ToolRegistry(build_file_tools(policy, trash)),
            policy=policy,
            audit=AuditLogger(self.audit),
            settings=AgentSettings(**{"check_answers": False, **agent_settings}),
            approver=approver,
            task_repository=self.tasks,
            on_task_update=self._on_update,
        )

    def _on_update(self, task):
        if not self.statuses or self.statuses[-1] != task.status:
            self.statuses.append(task.status)

    def events(self, task_id: str) -> list[str]:
        return [row["event"] for row in self.audit.for_task(task_id)]


@pytest.fixture
def make(policy, db, tmp_path):
    def _make(*responses, **kwargs) -> Harness:
        return Harness(ScriptedModel(*responses), policy, db, tmp_path / "Trash", **kwargs)
    return _make


async def test_read_only_task_completes(make, sandbox: Path):
    (sandbox / "a.txt").write_text("x")
    h = make(call("list_directory", directory_path=str(sandbox)), answer("You have a.txt."))
    task = await h.orchestrator.run("what's in my folder?")

    assert task.status == S.COMPLETED
    assert task.final_response == "You have a.txt."
    assert h.statuses == [S.RECEIVED, S.PLANNING, S.EXECUTING, S.VERIFYING, S.PLANNING,
                          S.COMPLETED]
    assert task.steps[0].status == S.COMPLETED and task.steps[0].result.verification_passed
    assert h.approvals == []
    # Tool output was fed back to the model.
    tool_msg = h.model.calls[1][-1]
    assert tool_msg["role"] == "tool" and "a.txt" in tool_msg["content"]
    # Persisted and audited.
    assert h.tasks.get(task.id).status == S.COMPLETED
    assert h.events(task.id) == ["TASK_RECEIVED", "TOOL_EXECUTED", "TASK_FINISHED"]


async def test_high_risk_requires_approval(make, sandbox: Path):
    (sandbox / "old.txt").write_text("x")
    h = make(call("permanently_delete_files", paths=[str(sandbox / "old.txt")]), answer("Deleted."))
    task = await h.orchestrator.run("delete old.txt permanently")

    assert task.status == S.COMPLETED
    assert h.approvals == ["permanently_delete_files"]
    assert S.AWAITING_APPROVAL in h.statuses
    assert not (sandbox / "old.txt").exists()
    assert "APPROVAL_GRANTED" in h.events(task.id)


async def test_denied_approval_cancels_and_does_not_execute(make, sandbox: Path):
    (sandbox / "keep.txt").write_text("x")
    h = make(call("permanently_delete_files", paths=[str(sandbox / "keep.txt")]),
             approve=False)
    task = await h.orchestrator.run("delete keep.txt")

    assert task.status == S.CANCELLED
    assert (sandbox / "keep.txt").exists()
    assert task.steps[0].status == S.CANCELLED
    assert "APPROVAL_DENIED" in h.events(task.id)
    assert "TOOL_EXECUTED" not in h.events(task.id)


async def test_low_risk_runs_without_approval(make, sandbox: Path):
    h = make(call("create_directory", directory_path=str(sandbox / "new")), answer("Created."))
    task = await h.orchestrator.run("make a folder")
    assert task.status == S.COMPLETED and (sandbox / "new").is_dir()
    assert task.steps[0].risk_level == RiskLevel.LOW and h.approvals == []


async def test_sandbox_violation_rejected_before_approval(make, tmp_path: Path):
    outside = tmp_path / "outside" / "f.txt"
    outside.write_text("x")
    h = make(call("permanently_delete_files", paths=[str(outside)]),
             answer("I can't access that location."))
    task = await h.orchestrator.run("delete that file")

    assert outside.exists()
    assert h.approvals == []  # user never asked to approve a denied path
    assert task.steps[0].status == S.FAILED
    assert "Access denied" in task.steps[0].result.error
    assert "Access denied" in h.model.calls[1][-1]["content"]
    assert task.status == S.COMPLETED  # the model explained the failure
    assert "TOOL_REJECTED" in h.events(task.id)


async def test_unknown_tool_and_bad_args_are_fed_back(make, sandbox: Path):
    h = make(call("format_disk"), call("list_directory"), answer("Sorry."))
    task = await h.orchestrator.run("hmm")
    assert task.status == S.COMPLETED
    assert "Unknown tool" in task.steps[0].result.error
    assert "directory_path" in task.steps[1].result.error


async def test_too_many_failures_fails_task(make, tmp_path: Path):
    bad = call("list_directory", directory_path="/etc")
    h = make(bad, bad, bad, answer("never reached"), max_failures=3)
    task = await h.orchestrator.run("look at /etc")
    assert task.status == S.FAILED and "failed tool calls" in task.error_message


async def test_max_iterations(make, sandbox: Path):
    loop_call = call("list_directory", directory_path=str(sandbox))
    h = make(*[loop_call] * 3, max_iterations=3)
    task = await h.orchestrator.run("loop forever")
    assert task.status == S.FAILED and "planning rounds" in task.error_message


async def test_llm_error_fails_task(make):
    h = make(OllamaError("connection refused"))
    task = await h.orchestrator.run("hi")
    assert task.status == S.FAILED and "connection refused" in task.final_response


class FlakyVerifyTool(BaseTool):
    name = "flaky"
    description = "Succeeds but never verifies."
    parameters = {"type": "object", "properties": {}}
    risk_level = RiskLevel.SAFE

    async def execute(self, tool_call_id, **kwargs):
        return ToolResult(tool_call_id=tool_call_id, success=True, data={})

    async def verify(self, execution_result):
        return False


async def test_verification_failure_triggers_replanning(make):
    h = make(call("flaky"), answer("It didn't take effect."))
    h.orchestrator.registry.register(FlakyVerifyTool())
    task = await h.orchestrator.run("do flaky thing")
    assert task.steps[0].status == S.FAILED
    assert "verification failed" in h.model.calls[1][-1]["content"]
    assert h.statuses[-2:] == [S.PLANNING, S.COMPLETED]
    assert "VERIFICATION_FAILED" in h.events(task.id)


async def test_cancellation_marks_task_cancelled(make, sandbox: Path):
    (sandbox / "f").write_text("x")
    gate = asyncio.Event()

    h = make(call("permanently_delete_files", paths=[str(sandbox / "f")]))

    async def slow_approver(task, call_req, tool):
        await gate.wait()
        return True

    h.orchestrator.approver = slow_approver
    runner = asyncio.create_task(h.orchestrator.run("delete f"))
    while S.AWAITING_APPROVAL not in h.statuses:
        await asyncio.sleep(0)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert h.statuses[-1] == S.CANCELLED and (sandbox / "f").exists()


async def test_follow_up_has_conversation_memory(make, sandbox: Path):
    h = make(answer("Hello!"), answer("You said hi."))
    await h.orchestrator.run("hi")
    await h.orchestrator.run("what did I say?")
    second = h.model.calls[1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]


class EscalatingTool(BaseTool):
    name = "press"
    description = "LOW by default, HIGH for scary targets."
    parameters = {"type": "object", "properties": {"label": {"type": "string"}}}
    risk_level = RiskLevel.LOW

    async def assess(self, arguments):
        if arguments.get("label") == "explode":
            raise RuntimeError("cannot inspect target")
        if "delete" in arguments.get("label", ""):
            return RiskLevel.HIGH, "button 'delete all'"
        return RiskLevel.SAFE, None  # must not lower below the class tier

    async def execute(self, tool_call_id, **kwargs):
        return ToolResult(tool_call_id=tool_call_id, success=True, data={})

    async def verify(self, execution_result):
        return True


async def test_dynamic_risk_escalation_requires_approval(make):
    h = make(call("press", label="delete all"), answer("ok"))
    h.orchestrator.registry.register(EscalatingTool())
    task = await h.orchestrator.run("press it")
    assert h.approvals == ["press"]
    assert task.steps[0].risk_level == RiskLevel.HIGH
    assert "button 'delete all'" in task.steps[0].description


async def test_assessment_cannot_lower_risk(make):
    h = make(call("press", label="hello"), answer("ok"))
    h.orchestrator.registry.register(EscalatingTool())
    task = await h.orchestrator.run("press it")
    assert task.steps[0].risk_level == RiskLevel.LOW and h.approvals == []


async def test_failed_assessment_fails_closed(make):
    h = make(call("press", label="explode"), approve=False)
    h.orchestrator.registry.register(EscalatingTool())
    task = await h.orchestrator.run("press it")
    assert h.approvals == ["press"] and task.status == S.CANCELLED


class MissingTargetTool(EscalatingTool):
    name = "press_missing"

    async def assess(self, arguments):
        from jarvis.security.validation import ValidationError
        raise ValidationError("No element named 'X'")


async def test_missing_target_is_fed_back_not_escalated(make):
    h = make(call("press_missing"), answer("It isn't there."))
    h.orchestrator.registry.register(MissingTargetTool())
    task = await h.orchestrator.run("press X")
    assert h.approvals == [] and task.status == S.COMPLETED
    assert "No element named" in task.steps[0].result.error


async def test_answer_check_keeps_supported_answer(make, sandbox: Path):
    (sandbox / "a.txt").write_text("x")
    h = make(call("list_directory", directory_path=str(sandbox)), answer("You have a.txt."),
             answer("OK"), check_answers=True)
    task = await h.orchestrator.run("what's here?")
    assert task.final_response == "You have a.txt."
    assert "check it against the tool results" in h.model.calls[-1][-1]["content"]


async def test_answer_check_replaces_unsupported_answer(make, sandbox: Path):
    (sandbox / "docs").mkdir()
    (sandbox / "docs" / "a.pdf").write_text("x")
    h = make(call("list_directory", directory_path=str(sandbox)),
             answer("There is an empty folder named docs."),
             answer("There is a folder named docs containing 1 item."), check_answers=True)
    task = await h.orchestrator.run("what's here?")
    assert task.final_response == "There is a folder named docs containing 1 item."


async def test_answer_check_skipped_without_tools(make):
    h = make(answer("Hello!"), check_answers=True)
    task = await h.orchestrator.run("hi")
    assert task.final_response == "Hello!" and len(h.model.calls) == 1


async def test_tool_less_action_claim_is_checked(make):
    h = make(answer("Done."), answer("I couldn't close YouTube: no tool was used."),
             check_answers=True)
    task = await h.orchestrator.run("Close YouTube")
    assert task.final_response == "I couldn't close YouTube: no tool was used."


def test_claims_action():
    from jarvis.agent.orchestrator import claims_action

    assert claims_action("Done.") and claims_action("I've closed YouTube")
    assert not claims_action("Two plus two is four.") and not claims_action("Hello!")
