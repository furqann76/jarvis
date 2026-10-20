"""Pre-execution validation of LLM-proposed tool calls."""

from typing import Any

from jsonschema import Draft202012Validator

from jarvis.security.permissions import SecurityPolicy, normalize_path
from jarvis.tools.base import BaseTool


class ValidationError(Exception):
    """A tool call was rejected before execution. The message is fed back to the LLM."""


def validate_arguments(tool: BaseTool, arguments: dict[str, Any]) -> None:
    validator = Draft202012Validator(tool.parameters)
    errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(
            f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors
        )
        raise ValidationError(f"Invalid arguments for {tool.name}: {details}")


def validate_paths(tool: BaseTool, arguments: dict[str, Any], policy: SecurityPolicy) -> None:
    for arg_name in tool.path_arguments:
        if arg_name not in arguments:
            continue
        value = arguments[arg_name]
        paths = value if isinstance(value, list) else [value]
        for raw in paths:
            if not policy.is_path_allowed(raw):
                raise ValidationError(
                    f"Access denied: {normalize_path(raw)} is outside the allowed "
                    f"directories or matches a deny pattern."
                )


def validate_urls(tool: BaseTool, arguments: dict[str, Any], policy: SecurityPolicy) -> None:
    for arg_name in tool.url_arguments:
        if arg_name in arguments:
            reason = policy.url_rejection_reason(str(arguments[arg_name]))
            if reason:
                raise ValidationError(f"Access denied: {reason}")


def validate_tool_call(
    tool: BaseTool, arguments: dict[str, Any], policy: SecurityPolicy
) -> None:
    validate_arguments(tool, arguments)
    validate_paths(tool, arguments, policy)
    validate_urls(tool, arguments, policy)
