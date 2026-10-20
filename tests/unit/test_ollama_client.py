import json

import httpx
import pytest

from jarvis.config import OllamaSettings
from jarvis.models.ollama_client import OllamaClient, OllamaError, parse_message


def test_parse_structured_tool_calls():
    resp = parse_message({
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "list_directory",
                                     "arguments": {"directory_path": "~/Downloads"}}}],
    })
    assert resp.tool_calls[0].name == "list_directory"
    assert resp.tool_calls[0].arguments == {"directory_path": "~/Downloads"}
    assert resp.tool_calls[0].id.startswith("call_")


def test_parse_string_arguments():
    resp = parse_message({"tool_calls": [{"id": "x", "function": {
        "name": "f", "arguments": '{"a": 1}'}}]})
    assert resp.tool_calls[0].arguments == {"a": 1} and resp.tool_calls[0].id == "x"


def test_parse_text_tool_call_fallback_and_think_stripping():
    content = (
        "<think>hmm</think>Let me check.\n<tool_call>\n"
        '{"name": "search_files", "arguments": {"directory_path": "~", "pattern": "*.pdf"}}'
        "\n</tool_call>"
    )
    resp = parse_message({"content": content})
    assert resp.content == "Let me check."
    assert resp.tool_calls[0].name == "search_files"


def test_parse_plain_answer():
    resp = parse_message({"content": "All done."})
    assert resp.content == "All done." and resp.tool_calls == []


async def test_chat_sends_tools_and_parses_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "hi"}})

    client = OllamaClient(OllamaSettings(model="m", think=False),
                          transport=httpx.MockTransport(handler))
    resp = await client.chat([{"role": "user", "content": "hey"}], tools=[{"type": "function"}])
    assert resp.content == "hi"
    assert seen["model"] == "m" and seen["stream"] is False and seen["think"] is False
    assert seen["tools"] == [{"type": "function"}]
    await client.aclose()


async def test_chat_http_error():
    client = OllamaClient(OllamaSettings(), transport=httpx.MockTransport(
        lambda r: httpx.Response(404, text="model not found")))
    with pytest.raises(OllamaError, match="404"):
        await client.chat([])
    await client.aclose()
