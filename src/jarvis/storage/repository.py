import json
from typing import Any

from pydantic import TypeAdapter

from jarvis.models.schemas import Task, TaskStep, utcnow
from jarvis.storage.database import Database

_steps_adapter = TypeAdapter(list[TaskStep])


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    def save(self, task: Task) -> None:
        steps_json = _steps_adapter.dump_json(task.steps).decode()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, user_prompt, status, final_response, error_message,
                                   steps_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    final_response = excluded.final_response,
                    error_message = excluded.error_message,
                    steps_json = excluded.steps_json,
                    updated_at = excluded.updated_at
                """,
                (
                    task.id,
                    task.user_prompt,
                    task.status.value,
                    task.final_response,
                    task.error_message,
                    steps_json,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )

    def get(self, task_id: str) -> Task | None:
        rows = self.db.query("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return self._row_to_task(rows[0]) if rows else None

    def recent(self, limit: int = 50) -> list[Task]:
        rows = self.db.query("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._row_to_task(r) for r in rows]

    @staticmethod
    def _row_to_task(row) -> Task:
        return Task(
            id=row["id"],
            user_prompt=row["user_prompt"],
            status=row["status"],
            final_response=row["final_response"],
            error_message=row["error_message"],
            steps=_steps_adapter.validate_json(row["steps_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class AuditRepository:
    def __init__(self, db: Database):
        self.db = db

    def append(
        self,
        event: str,
        *,
        task_id: str | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        risk_level: str | None = None,
        arguments: dict[str, Any] | None = None,
        detail: str | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (timestamp, task_id, tool_call_id, tool_name, risk_level,
                                       event, arguments, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utcnow().isoformat(),
                    task_id,
                    tool_call_id,
                    tool_name,
                    risk_level,
                    event,
                    json.dumps(arguments, default=str) if arguments is not None else None,
                    detail,
                ),
            )

    def for_task(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM audit_log WHERE task_id = ? ORDER BY id", (task_id,))
        return [dict(r) for r in rows]
