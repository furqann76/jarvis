from pathlib import Path

from jarvis.models.schemas import Task, TaskStatus, TaskStep, ToolCallRequest, ToolResult
from jarvis.storage.database import Database
from jarvis.storage.repository import AuditRepository, TaskRepository


def test_wal_mode(tmp_path: Path):
    db = Database(tmp_path / "j.db")
    assert db.query("PRAGMA journal_mode")[0][0] == "wal"
    db.close()


def test_task_roundtrip_and_upsert(db: Database):
    repo = TaskRepository(db)
    task = Task(id="t1", user_prompt="list files")
    repo.save(task)
    task.status = TaskStatus.COMPLETED
    task.final_response = "done"
    task.steps.append(TaskStep(
        step_id=1, description="list",
        tool_call=ToolCallRequest(id="c", name="list_directory", arguments={"directory_path": "~"}),
        result=ToolResult(tool_call_id="c", success=True, data={"items": []}),
        status=TaskStatus.COMPLETED,
    ))
    repo.save(task)

    loaded = repo.get("t1")
    assert loaded == task
    assert [t.id for t in repo.recent()] == ["t1"]
    assert repo.get("missing") is None


def test_audit_append(db: Database):
    repo = AuditRepository(db)
    repo.append("TOOL_EXECUTED", task_id="t1", tool_name="x", arguments={"a": 1})
    repo.append("TASK_FINISHED", task_id="t1")
    rows = repo.for_task("t1")
    assert [r["event"] for r in rows] == ["TOOL_EXECUTED", "TASK_FINISHED"]
    assert rows[0]["arguments"] == '{"a": 1}'
