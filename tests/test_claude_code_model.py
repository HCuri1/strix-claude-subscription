"""Tests for the claude-code/ model route (Claude Code as a completion endpoint)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from agents import FunctionTool, ModelSettings
from agents.models.interface import ModelTracing
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from strix.config import codex, loader
from strix.config.models import (
    StrixProvider,
    routes_through_litellm,
    uses_chat_completions_tool_schema,
)
from strix.llm import claude_code
from strix.llm.claude_code import (
    ClaudeCodeError,
    ClaudeCodeModel,
    build_content_blocks,
    claude_code_model,
)


def test_claude_code_model_parses_prefix() -> None:
    assert claude_code_model("claude-code/sonnet") == "sonnet"
    assert claude_code_model("Claude-Code/claude-opus-5-5") == "claude-opus-5-5"
    assert claude_code_model("claude-code/") is None
    assert claude_code_model("anthropic/claude-sonnet-5-5") is None
    assert claude_code_model(None) is None


def test_subscription_accounting_treats_claude_code_as_zero_cost() -> None:
    assert codex.auth_mode("claude-code/sonnet") == "subscription"
    assert codex.auth_mode("anthropic/claude-sonnet-5-5") == "api_key"


def test_route_flags() -> None:
    settings = loader.load_settings()
    assert uses_chat_completions_tool_schema("claude-code/sonnet", settings) is True
    assert routes_through_litellm("claude-code/sonnet") is False


def test_provider_routes_to_claude_code_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIX_LLM", "claude-code/haiku")
    monkeypatch.setattr(loader, "_cached", None)
    monkeypatch.setattr(loader, "_override", None)
    model = StrixProvider().get_model("claude-code/haiku")
    inner = model
    while not isinstance(inner, ClaudeCodeModel):
        inner = inner._inner  # type: ignore[attr-defined]
    assert inner.model == "haiku"


# --- history -> content blocks -------------------------------------------------


HISTORY: list[dict[str, Any]] = [
    {"role": "user", "content": "scan it"},
    {"type": "function_call", "call_id": "c1", "name": "exec_command", "arguments": '{"cmd":"ls"}'},
    {"type": "function_call_output", "call_id": "c1", "output": "a.txt"},
]


def test_blocks_mirror_history_one_per_item() -> None:
    blocks = build_content_blocks(HISTORY)
    assert len(blocks) == 3
    assert blocks[1]["text"].startswith('<tool_call id="c1" name="mcp__strix__exec_command">')
    assert "a.txt" in blocks[2]["text"]


def test_blocks_are_append_only_between_turns() -> None:
    """Unchanged items must stay byte-identical so the prompt cache keeps hitting."""
    before = build_content_blocks(HISTORY)
    after = build_content_blocks([*HISTORY, {"role": "user", "content": "go on"}])
    assert after[: len(before)] == before
    assert len(after) == len(before) + 1


def test_image_output_becomes_native_image_block() -> None:
    items = [
        {
            "type": "function_call_output",
            "call_id": "c2",
            "output": [
                {"type": "input_text", "text": "screenshot"},
                {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
            ],
        }
    ]
    blocks = build_content_blocks(items)
    assert blocks[0]["type"] == "text"
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }


def test_string_input_and_empty_history() -> None:
    assert build_content_blocks("hello") == [{"type": "text", "text": "hello"}]
    assert build_content_blocks([])[0]["type"] == "text"


# --- get_response ----------------------------------------------------------------


def _tool() -> FunctionTool:
    async def invoke(_ctx: Any, _args: str) -> str:
        return "unused"

    return FunctionTool(
        name="exec_command",
        description="Run a command",
        params_json_schema={
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
        on_invoke_tool=invoke,
        strict_json_schema=False,
    )


def _result(**kw: Any) -> ResultMessage:
    base: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "s",
        "usage": {
            "input_tokens": 3,
            "cache_read_input_tokens": 90,
            "cache_creation_input_tokens": 7,
            "output_tokens": 11,
        },
    }
    return ResultMessage(**{**base, **kw})


def _fake_query(*messages: Any, then_raise: BaseException | None = None) -> Any:
    seen: dict[str, Any] = {}

    async def query(*, prompt: Any, options: Any) -> Any:
        seen["options"] = options
        seen["prompt"] = [m async for m in prompt]
        for message in messages:
            yield message
        if then_raise is not None:
            raise then_raise

    query.seen = seen  # type: ignore[attr-defined]
    return query


async def _ask(model: ClaudeCodeModel, tools: list[Any] | None = None) -> Any:
    return await model.get_response(
        "system text",
        HISTORY,
        ModelSettings(),
        tools if tools is not None else [_tool()],
        None,
        [],
        ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )


async def test_tool_use_becomes_function_call(monkeypatch: pytest.MonkeyPatch) -> None:
    query = _fake_query(
        AssistantMessage(content=[TextBlock(text="thinking out loud")], model="m"),
        AssistantMessage(
            content=[
                ToolUseBlock(id="toolu_1", name="mcp__strix__exec_command", input={"cmd": "id"})
            ],
            model="m",
        ),
        _result(is_error=True, subtype="error_max_turns"),
        then_raise=ResultError("max turns", {"subtype": "error_max_turns"}),
    )
    monkeypatch.setattr("claude_agent_sdk.query", query)

    response = await _ask(ClaudeCodeModel("haiku"))

    kinds = [type(item).__name__ for item in response.output]
    assert kinds == ["ResponseOutputMessage", "ResponseFunctionToolCall"]
    call = response.output[1]
    assert (call.name, call.call_id) == ("exec_command", "toolu_1")  # prefix stripped
    assert json.loads(call.arguments) == {"cmd": "id"}
    usage = response.usage
    assert usage.input_tokens == 100  # fresh + cache read + cache write
    assert usage.input_tokens_details.cached_tokens == 90
    assert usage.output_tokens == 11
    options = query.seen["options"]
    assert options.tools == [] and options.max_turns == 1 and options.setting_sources == []
    # Passed via a file, not argv, so a large system prompt doesn't hit
    # Linux's MAX_ARG_STRLEN (OSError: Argument list too long).
    assert options.system_prompt["type"] == "file"
    assert Path(options.system_prompt["path"]).read_text() == "system text"
    assert len(query.seen["prompt"]) == 1  # a single user message carrying the blocks


async def test_only_first_tool_use_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    first = ToolUseBlock(id="t1", name="mcp__strix__exec_command", input={"cmd": "a"})
    second = ToolUseBlock(id="t2", name="mcp__strix__exec_command", input={"cmd": "b"})
    monkeypatch.setattr(
        "claude_agent_sdk.query",
        _fake_query(AssistantMessage(content=[first, second], model="m"), _result()),
    )
    response = await _ask(ClaudeCodeModel("haiku"))
    calls = [i for i in response.output if type(i).__name__ == "ResponseFunctionToolCall"]
    assert [c.call_id for c in calls] == ["t1"]


async def test_text_only_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "claude_agent_sdk.query",
        _fake_query(AssistantMessage(content=[TextBlock(text="done")], model="m"), _result()),
    )
    response = await _ask(ClaudeCodeModel("haiku"), tools=[])
    assert [type(i).__name__ for i in response.output] == ["ResponseOutputMessage"]


async def test_rate_limit_maps_to_openai_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    limit = SimpleNamespace(status="rejected", resets_at=1234)
    monkeypatch.setattr(
        "claude_agent_sdk.query",
        _fake_query(
            RateLimitEvent(rate_limit_info=limit, uuid="u", session_id="s"),  # type: ignore[arg-type]
            _result(is_error=True, subtype="success", api_error_status=429, result="limit"),
            then_raise=ResultError("boom", {"subtype": "success", "api_error_status": 429}),
        ),
    )
    with pytest.raises(openai.RateLimitError) as excinfo:
        await _ask(ClaudeCodeModel("haiku"))
    assert isinstance(excinfo.value.response, httpx.Response)
    assert "1234" in str(excinfo.value)


async def test_not_signed_in_gives_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "claude_agent_sdk.query",
        _fake_query(
            _result(is_error=True, subtype="success", result="Not logged in · Please run /login"),
            then_raise=ResultError("x", {"subtype": "success", "result": "Not logged in"}),
        ),
    )
    with pytest.raises(ClaudeCodeError, match="claude auth login"):
        await _ask(ClaudeCodeModel("haiku"))


async def test_stream_response_yields_single_completed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "claude_agent_sdk.query",
        _fake_query(AssistantMessage(content=[TextBlock(text="hi")], model="m"), _result()),
    )
    events = [
        e
        async for e in ClaudeCodeModel("haiku").stream_response(
            "s",
            HISTORY,
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    ]
    assert [e.type for e in events] == ["response.completed"]
    assert events[0].response.usage.input_tokens == 100


def test_check_available_reports_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_code.shutil, "which", lambda _name: None)
    problem, _ = claude_code.check_available()
    assert problem and "not found" in problem


async def test_refusal_stop_reason_raises_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "claude_agent_sdk.query",
        _fake_query(
            AssistantMessage(
                content=[TextBlock(text="I can't help with that.")],
                model="m",
                stop_reason="refusal",
            ),
            _result(),
        ),
    )
    with pytest.raises(claude_code.ClaudeCodeRefusalError, match="haiku"):
        await _ask(ClaudeCodeModel("haiku"))


def test_billing_risk_env_vars_detects_known_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in claude_code.BILLING_RISK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert claude_code.billing_risk_env_vars() == []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert claude_code.billing_risk_env_vars() == ["ANTHROPIC_API_KEY"]


def test_concurrency_and_timeout_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIX_CLAUDE_CODE_CONCURRENCY", "7")
    monkeypatch.setenv("STRIX_CLAUDE_CODE_TIMEOUT", "42")
    monkeypatch.setattr(loader, "_cached", None)
    monkeypatch.setattr(loader, "_override", None)
    settings = loader.load_settings()
    assert settings.llm.claude_code_concurrency == 7
    assert settings.llm.claude_code_timeout == 42.0
