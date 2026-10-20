"""Async client for Ollama's /api/chat endpoint with OpenAI-style tool calling."""

import json
import re
import uuid
from typing import Any

import httpx
from pydantic import BaseModel, Field

from jarvis.config import OllamaSettings
from jarvis.models.schemas import ToolCallRequest

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# Qwen models occasionally emit tool calls as text instead of structured tool_calls.
_TEXT_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


class OllamaError(Exception):
    pass


class ChatResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    raw_message: dict[str, Any] = Field(default_factory=dict)


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


def _coerce_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise OllamaError(f"Model returned non-JSON tool arguments: {raw!r}") from e
        if isinstance(parsed, dict):
            return parsed
    return {}


def parse_message(message: dict[str, Any]) -> ChatResponse:
    content = _THINK_RE.sub("", message.get("content") or "").strip()
    calls: list[ToolCallRequest] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {})
        calls.append(
            ToolCallRequest(
                id=tc.get("id") or _new_call_id(),
                name=fn.get("name", ""),
                arguments=_coerce_arguments(fn.get("arguments")),
            )
        )
    if not calls:
        for match in _TEXT_TOOL_CALL_RE.finditer(content):
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            calls.append(
                ToolCallRequest(
                    id=_new_call_id(),
                    name=payload.get("name", ""),
                    arguments=_coerce_arguments(payload.get("arguments")),
                )
            )
        if calls:
            content = _TEXT_TOOL_CALL_RE.sub("", content).strip()
    return ChatResponse(content=content, tool_calls=calls, raw_message=message)


class OllamaClient:
    def __init__(self, settings: OllamaSettings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.host,
            timeout=httpx.Timeout(settings.request_timeout_s, connect=5.0),
            transport=transport,
        )

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.settings.temperature,
                "num_ctx": self.settings.num_ctx,
            },
        }
        if tools:
            payload["tools"] = tools
        if self.settings.think is not None:
            payload["think"] = self.settings.think
        try:
            response = await self._client.post("/api/chat", json=payload)
        except httpx.HTTPError as e:
            raise OllamaError(
                f"Cannot reach Ollama at {self.settings.host}: {e}. Is `ollama serve` running?"
            ) from e
        if response.status_code != 200:
            raise OllamaError(f"Ollama returned HTTP {response.status_code}: {response.text}")
        body = response.json()
        if "error" in body:
            raise OllamaError(f"Ollama error: {body['error']}")
        return parse_message(body.get("message", {}))

    async def list_models(self) -> list[str]:
        try:
            response = await self._client.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise OllamaError(f"Cannot reach Ollama at {self.settings.host}: {e}") from e
        return [m["name"] for m in response.json().get("models", [])]

    async def aclose(self) -> None:
        await self._client.aclose()
