from abc import ABC, abstractmethod
from typing import Any, ClassVar

from jarvis.models.schemas import RiskLevel, ToolResult


class BaseTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict[str, Any]]  # JSON Schema definition sent to Ollama
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    # Argument names holding a path (str) or list of paths. The validation layer checks
    # these against the SecurityPolicy *before* approval/execution.
    path_arguments: ClassVar[tuple[str, ...]] = ()
    # Argument names holding a URL, checked against the policy's URL rules.
    url_arguments: ClassVar[tuple[str, ...]] = ()

    async def assess(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str | None]:
        """Risk of this specific call, plus optional context for the user.

        Called after validation, before approval. Tools whose danger depends on the target
        (e.g. a "Place order" button) override this to escalate. The orchestrator never
        lets the result drop below the class-level ``risk_level``. Raise
        security.validation.ValidationError when the target does not exist (the error goes
        back to the model); any other exception makes the call require approval.
        """
        return self.risk_level, None

    @abstractmethod
    async def execute(self, tool_call_id: str, **kwargs: Any) -> ToolResult:
        """Executes the tool logic with provided validated arguments."""

    @abstractmethod
    async def verify(self, execution_result: ToolResult) -> bool:
        """Verifies the action achieved its intended state on the system.

        Implementations inspect the real system, using whatever ``execute`` recorded in
        ``execution_result.data``, rather than trusting ``execution_result.success``.
        """

    def describe_call(self, arguments: dict[str, Any]) -> str:
        """Short human-readable summary shown in the timeline and approval dialog."""
        args = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
        return f"{self.name}({args})"

    def ollama_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
