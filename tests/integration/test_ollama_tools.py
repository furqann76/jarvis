"""End-to-end against the real local Ollama model. Skipped when Ollama is unreachable.

Run with: pytest -m integration tests/integration
"""

from pathlib import Path

import httpx
import pytest

from jarvis.agent.orchestrator import Orchestrator
from jarvis.agent.planner import Planner
from jarvis.agent.tool_registry import ToolRegistry
from jarvis.config import AgentSettings, load_settings
from jarvis.models.ollama_client import OllamaClient
from jarvis.models.schemas import TaskStatus
from jarvis.security.audit import AuditLogger
from jarvis.security.permissions import SecurityPolicy
from jarvis.storage.repository import AuditRepository
from jarvis.tools.files import build_file_tools

pytestmark = pytest.mark.integration

SETTINGS = load_settings()


def _ollama_ready() -> bool:
    try:
        tags = httpx.get(f"{SETTINGS.ollama.host}/api/tags", timeout=2).json()
    except (httpx.HTTPError, ValueError):
        return False
    return any(m["name"] == SETTINGS.ollama.model for m in tags.get("models", []))


requires_ollama = pytest.mark.skipif(
    not _ollama_ready(), reason=f"Ollama with {SETTINGS.ollama.model} not available"
)


def _orchestrator(sandbox: Path, db, approve: bool):
    policy = SecurityPolicy([str(sandbox)])
    client = OllamaClient(SETTINGS.ollama.model_copy(update={"temperature": 0.0}))

    async def approver(task, call, tool):
        return approve

    orch = Orchestrator(
        planner=Planner(client, [str(sandbox)]),
        registry=ToolRegistry(build_file_tools(policy, sandbox.parent / "Trash")),
        policy=policy,
        audit=AuditLogger(AuditRepository(db)),
        settings=AgentSettings(),
        approver=approver,
    )
    return orch, client


@requires_ollama
async def test_model_lists_directory(sandbox: Path, db):
    (sandbox / "quarterly_report.pdf").write_bytes(b"%PDF")
    (sandbox / "holiday.jpg").write_bytes(b"\xff\xd8")
    orch, client = _orchestrator(sandbox, db, approve=False)
    try:
        task = await orch.run(f"What files are in {sandbox}?")
    finally:
        await client.aclose()

    assert task.status == TaskStatus.COMPLETED, task.error_message
    assert any(s.tool_call and s.tool_call.name in ("list_directory", "search_files")
               for s in task.steps)
    assert "quarterly_report" in task.final_response


@requires_ollama
async def test_model_creates_directory(sandbox: Path, db):
    orch, client = _orchestrator(sandbox, db, approve=False)
    try:
        task = await orch.run(f"Create a folder named 'projects' inside {sandbox}")
    finally:
        await client.aclose()
    assert task.status == TaskStatus.COMPLETED, task.error_message
    assert (sandbox / "projects").is_dir()


@requires_ollama
async def test_denied_high_risk_action_is_not_executed(sandbox: Path, db):
    target = sandbox / "important.txt"
    target.write_text("do not delete")
    orch, client = _orchestrator(sandbox, db, approve=False)
    try:
        task = await orch.run(f"Permanently delete the file {target}. Do not use the trash.")
    finally:
        await client.aclose()
    assert target.exists()
    assert task.status in (TaskStatus.CANCELLED, TaskStatus.COMPLETED)
