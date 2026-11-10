"""Claude Code backed model: run Strix agents on the user's own Claude login.

Proof of concept. ``STRIX_LLM=claude-code/<model>`` (``sonnet``, ``opus``,
``haiku`` or a full model id) routes every model call through the *unmodified*
``claude`` binary via the Claude Agent SDK, authenticated by whatever the user
signed in with (``claude auth login``). Strix never reads, stores or forwards
the OAuth credentials, which is the line Anthropic's terms draw: sign-in
happens through Anthropic's own flow, in Anthropic's own binary.

How a Strix turn maps onto one Claude Code run
----------------------------------------------
Strix owns the agent loop (tool execution, sessions, compaction, budgets); the
model only has to answer one turn: "given this history, what is the next
assistant message / tool call?". So each ``get_response`` spawns one headless
Claude Code run configured as a plain completion endpoint:

* every Claude Code built-in tool is disabled and settings/CLAUDE.md are
  ignored, so the run sees only Strix's system prompt;
* Strix's tools are exposed as *stub* MCP tools carrying the real JSON
  schemas. The model calls one natively; ``max_turns=1`` plus permission
  denial guarantee the stub body never executes. We read the ``tool_use``
  block and hand it back to Strix as a ``function_call`` item, and Strix runs
  the real tool in its sandbox;
* the history is replayed as a transcript in the prompt each turn. The prefix
  is stable between turns, so Claude's prompt cache applies.

Known PoC limits: images in tool output are dropped; history is flattened to
text rather than native tool_use/tool_result blocks; no token streaming.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
import subprocess  # nosec B404 - launches the claude binary
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tool import FunctionTool
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseUsage,
)
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agents.agent_output import AgentOutputSchemaBase
    from agents.handoffs import Handoff
    from agents.items import TResponseInputItem, TResponseStreamEvent
    from agents.model_settings import ModelSettings
    from agents.models.interface import ModelTracing
    from agents.tool import Tool
    from openai.types.responses import ResponsePromptParam


logger = logging.getLogger(__name__)

PREFIX = "claude-code/"
MCP_SERVER_NAME = "strix"
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

# How many `claude` processes may run at once. Strix runs many agents in
# parallel; each model call is a process, and the subscription has its own rate
# limits, so keep this modest.
# Best-effort context/output limits for Claude Code's model *aliases*
# (``sonnet``, ``opus``, ``haiku``), which carry no LiteLLM catalog entry at
# all - unlike full versioned ids, which are fixed by skipping the prefixed
# lookup in context_budget._model_info instead. Anthropic may repoint an
# alias to a different underlying model at any time; when that happens these
# numbers go stale until updated here by hand. This is a safety-net
# improvement over STRIX_CONTEXT_FALLBACK_TOKENS (200_000 / 8_192 default),
# not a source of truth.
ALIAS_CONTEXT_LIMITS: dict[str, tuple[int, int]] = {
    "sonnet": (1_000_000, 128_000),
    "opus": (1_000_000, 128_000),
    "haiku": (1_000_000, 128_000),
}


def claude_code_model(model_name: str | None) -> str | None:
    """The model slug behind a ``claude-code/<model>`` STRIX_LLM, or None."""
    name = (model_name or "").strip()
    if not name.lower().startswith(PREFIX):
        return None
    return name[len(PREFIX) :] or None


class ClaudeCodeError(RuntimeError):
    """The Claude Code run failed in a way the caller should see verbatim."""


class ClaudeCodeRefusalError(ClaudeCodeError):
    """The underlying Claude model declined this turn for safety reasons."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(
            f"Claude ({model}) declined to continue this turn (safety refusal). "
            "Try a different claude-code/<model> (e.g. claude-code/opus or "
            "claude-code/sonnet) and re-run."
        )


# Env vars that could make the `claude` subprocess bill real API usage
# instead of using the subscription login: the Agent SDK passes almost the
# whole parent environment through to the `claude` child process, so any of
# these silently override `claude auth login` without Strix ever seeing it.
BILLING_RISK_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


def billing_risk_env_vars() -> list[str]:
    """Env vars set in this process that risk real API billing instead of
    the subscription route. Detection only - Strix never reads, clears or
    overrides these; the caller decides how to warn.
    """
    return [name for name in BILLING_RISK_ENV_VARS if os.environ.get(name)]


def check_available() -> tuple[str | None, str]:
    """Check the ``claude`` binary is installed and signed in.

    Returns ``(problem, detail)``: ``problem`` is a user-facing message (None
    when everything is fine) and ``detail`` describes the account when known.
    """
    path = shutil.which("claude")
    if path is None:
        return (
            (
                "The `claude` binary was not found on PATH. Install Claude Code "
                "(https://code.claude.com) and sign in with `claude auth login`."
            ),
            "",
        )
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, no shell
            [path, "auth", "status"], capture_output=True, text=True, timeout=30, check=False
        )
        status = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return f"Could not read Claude Code sign-in status: {exc}", ""
    if not status.get("loggedIn"):
        return "Claude Code is not signed in. Run `claude auth login` first.", ""
    plan = status.get("subscriptionType") or status.get("authMethod") or "unknown plan"
    return None, f"{status.get('authMethod', '?')} / {plan}"


_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _concurrency_limit() -> asyncio.Semaphore:
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        from strix.config import load_settings  # noqa: PLC0415

        limit = max(1, load_settings().llm.claude_code_concurrency)
        _semaphore, _semaphore_loop = asyncio.Semaphore(limit), loop
    return _semaphore


_scratch_dir: str | None = None


def _isolated_cwd() -> str:
    """An empty working directory, so no project CLAUDE.md/.mcp.json is picked up."""
    global _scratch_dir  # noqa: PLW0603
    if _scratch_dir is None or not Path(_scratch_dir).is_dir():
        _scratch_dir = tempfile.mkdtemp(prefix="strix-claude-code-")
    return _scratch_dir


def _system_prompt_arg(cwd: str, system: str | None) -> str | dict[str, str]:
    """Pass the system prompt through a file, not argv.

    Strix's system prompt runs tens of KB (full tool catalogue + skills), well
    past Linux's ``MAX_ARG_STRLEN`` (128 KiB per argv entry). The SDK puts a
    plain-string ``system_prompt`` on the ``claude`` command line as
    ``--system-prompt <string>``, which raises ``OSError: [Errno 7] Argument
    list too long`` once the prompt is long enough. ``{"type": "file", ...}``
    makes it pass ``--system-prompt-file <path>`` instead.
    """
    if not system:
        return ""
    path = Path(cwd) / "system_prompt.txt"
    path.write_text(system, encoding="utf-8")
    return {"type": "file", "path": str(path)}


# --------------------------------------------------------------------------- #
# History -> transcript
# --------------------------------------------------------------------------- #


def _as_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dict(dump(exclude_none=True))
    return {}


def _image_block(data: dict[str, Any]) -> dict[str, Any] | None:
    url = str(data.get("image_url") or "")
    if url.startswith("data:") and ";base64," in url:
        head, _, payload = url.partition(",")
        media_type = head[len("data:") :].split(";")[0] or "image/png"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": payload},
        }
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _content_blocks(content: Any, header: str, footer: str) -> list[dict[str, Any]]:
    """One text block (text wrapped in header/footer) plus any image blocks."""
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for block in content if isinstance(content, list) else [content]:
        if block is None:
            continue
        data = _as_dict(block) if not isinstance(block, str) else {"type": "text", "text": block}
        kind = data.get("type")
        if kind in {"input_image", "image"}:
            image = _image_block(data)
            if image is not None:
                images.append(image)
            else:
                texts.append("[image omitted]")
        elif kind == "refusal":
            texts.append(str(data.get("refusal", "")))
        elif "text" in data:
            texts.append(str(data["text"]))
    text = "\n".join(t for t in texts if t)
    if not text and not images:
        return []
    return [{"type": "text", "text": f"{header}{text}{footer}"}, *images]


def build_content_blocks(items: str | list[Any]) -> list[dict[str, Any]]:
    """Render Responses-format input items as user content blocks, one per item.

    The prompt is a single user message whose blocks mirror the history. Blocks
    are append-only between turns and byte-identical for unchanged items, which
    is what lets Claude's prompt cache serve the whole earlier history: a
    wrapper block or trailing instruction that moved would break the prefix.
    """
    if isinstance(items, str):
        return [{"type": "text", "text": items}]

    blocks: list[dict[str, Any]] = []
    for raw in items:
        item = _as_dict(raw)
        kind = item.get("type")
        role = item.get("role")
        if kind == "function_call":
            name = str(item.get("name", ""))
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f'<tool_call id="{item.get("call_id", "")}" '
                        f'name="{MCP_TOOL_PREFIX}{name}">\n'
                        f"{item.get('arguments', '')}\n</tool_call>"
                    ),
                }
            )
        elif kind == "function_call_output":
            blocks.extend(
                _content_blocks(
                    item.get("output"),
                    f'<tool_result id="{item.get("call_id", "")}">\n',
                    "\n</tool_result>",
                )
                or [
                    {
                        "type": "text",
                        "text": f'<tool_result id="{item.get("call_id", "")}"></tool_result>',
                    }
                ]
            )
        elif kind in {None, "message"} and role:
            blocks.extend(
                _content_blocks(item.get("content"), f'<message role="{role}">\n', "\n</message>")
            )
        # reasoning items and anything else carry nothing the model needs.

    return blocks or [{"type": "text", "text": "(no messages)"}]


async def _prompt_stream(blocks: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    yield {
        "type": "user",
        "message": {"role": "user", "content": blocks},
        "parent_tool_use_id": None,
    }


# --------------------------------------------------------------------------- #
# Tools -> stub MCP server
# --------------------------------------------------------------------------- #


def _function_tools(tools: list[Tool]) -> list[FunctionTool]:
    usable: list[FunctionTool] = []
    for tool in tools:
        if isinstance(tool, FunctionTool):
            usable.append(tool)
        else:
            logger.warning(
                "claude-code route cannot offer tool %r (%s); only function tools are supported",
                getattr(tool, "name", "?"),
                type(tool).__name__,
            )
    return usable


def _build_stub_server(tools: list[FunctionTool]) -> Any:
    from claude_agent_sdk import create_sdk_mcp_server, tool  # noqa: PLC0415 - lazy

    async def _never_runs(_args: dict[str, Any]) -> dict[str, Any]:
        # Reaching this means a permission check failed open: refuse loudly.
        return {
            "content": [{"type": "text", "text": "Tool execution is handled by the caller."}],
            "is_error": True,
        }

    stubs = [
        tool(
            t.name,
            t.description or t.name,
            t.params_json_schema or {"type": "object", "properties": {}},
        )(_never_runs)
        for t in tools
    ]
    return create_sdk_mcp_server(MCP_SERVER_NAME, tools=stubs)


# --------------------------------------------------------------------------- #
# Result mapping
# --------------------------------------------------------------------------- #


def _input_details(cached: int, created: int) -> InputTokensDetails:
    # `cache_write_tokens` is required by newer openai releases and unknown to older ones.
    fields = InputTokensDetails.model_fields
    payload = {"cached_tokens": cached}
    if "cache_write_tokens" in fields:
        payload["cache_write_tokens"] = created
    return InputTokensDetails.model_validate(payload)


def _usage_from(raw: dict[str, Any] | None) -> Usage:
    raw = raw or {}
    fresh = int(raw.get("input_tokens") or 0)
    cached = int(raw.get("cache_read_input_tokens") or 0)
    created = int(raw.get("cache_creation_input_tokens") or 0)
    output = int(raw.get("output_tokens") or 0)
    total_in = fresh + cached + created
    return Usage(
        requests=1,
        input_tokens=total_in,
        output_tokens=output,
        total_tokens=total_in + output,
        input_tokens_details=_input_details(cached, created),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )


def _raise_for_failure(result: Any, rate_limited: Any | None) -> None:
    """Translate a failed run into an exception the Strix retry logic understands."""
    text = " ".join(
        str(x)
        for x in [getattr(result, "result", None), *(getattr(result, "errors", None) or [])]
        if x
    ).strip()
    status = getattr(result, "api_error_status", None)
    lowered = text.lower()
    if status == 429 or (
        rate_limited is not None and getattr(rate_limited, "status", "") == "rejected"
    ):
        import httpx  # noqa: PLC0415 - only on the error path
        import openai  # noqa: PLC0415

        resets = getattr(rate_limited, "resets_at", None)
        hint = f" (window resets at unix time {resets})" if resets else ""
        request = httpx.Request("POST", "https://claude-code.invalid/")
        raise openai.RateLimitError(
            f"Claude subscription limit reached{hint}: {text or 'rate limited'}",
            response=httpx.Response(429, request=request),
            body=None,
        )
    if "not logged in" in lowered or "/login" in lowered or status in {401, 403}:
        raise ClaudeCodeError(
            f"Claude Code is not signed in. Run `claude auth login` and retry. Details: {text}"
        )
    raise ClaudeCodeError(f"Claude Code run failed ({getattr(result, 'subtype', '?')}): {text}")


@dataclasses.dataclass
class _Turn:
    """What one Claude Code run produced."""

    texts: list[str] = dataclasses.field(default_factory=list)
    tool_use: Any = None
    result: Any = None
    rate_limit: Any = None
    last_usage: dict[str, Any] | None = None
    stop_reason: str | None = None


def _build_options(
    model: str, system: str | None, tools: list[FunctionTool], effort: str | None
) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions  # noqa: PLC0415 - lazy: slow, SDK-only

    cwd = _isolated_cwd()
    kwargs: dict[str, Any] = {
        "model": model,
        "system_prompt": _system_prompt_arg(cwd, system),
        "tools": [],  # no Claude Code built-in tools
        "setting_sources": [],  # ignore ~/.claude and project settings
        "strict_mcp_config": True,
        "max_turns": 1,
        "cwd": cwd,
        "verbatim_prompts": True,
    }
    if tools:
        kwargs["mcp_servers"] = {MCP_SERVER_NAME: _build_stub_server(tools)}
    if effort in {"low", "medium", "high", "xhigh", "max"}:
        kwargs["effort"] = effort
    return ClaudeAgentOptions(**kwargs)


async def _run_turn(blocks: list[dict[str, Any]], options: Any) -> _Turn:
    from claude_agent_sdk import (  # noqa: PLC0415 - lazy: slow, SDK-only
        AssistantMessage,
        RateLimitEvent,
        ResultError,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )

    turn = _Turn()
    try:
        async for msg in query(prompt=_prompt_stream(blocks), options=options):
            if isinstance(msg, AssistantMessage):
                turn.last_usage = msg.usage or turn.last_usage
                turn.stop_reason = msg.stop_reason or turn.stop_reason
                for block in msg.content:
                    if turn.tool_use is not None:
                        break  # one tool call per turn, like parallel_tool_calls=False
                    if isinstance(block, TextBlock):
                        turn.texts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        turn.tool_use = block
            elif isinstance(msg, ResultMessage):
                turn.result = msg
            elif isinstance(msg, RateLimitEvent):
                turn.rate_limit = msg.rate_limit_info
    except ResultError as exc:
        # `max_turns=1` ends a tool-calling run with error_max_turns, raised after
        # the result. That is the expected exit once we have a tool_use; anything
        # else is a real failure, mapped to what Strix knows how to retry on.
        if turn.tool_use is None:
            _raise_for_failure(turn.result if turn.result is not None else exc, turn.rate_limit)
            raise
    if turn.tool_use is None and turn.result is not None and turn.result.is_error:
        _raise_for_failure(turn.result, turn.rate_limit)
    if turn.tool_use is None and turn.stop_reason == "refusal":
        raise ClaudeCodeRefusalError(getattr(options, "model", "?"))
    return turn


def _to_output(turn: _Turn, known_tools: set[str]) -> list[Any]:
    output: list[Any] = []
    text = "".join(turn.texts).strip()
    if text:
        output.append(
            ResponseOutputMessage(
                id=f"msg_{int(time.time() * 1000)}",
                role="assistant",
                status="completed",
                type="message",
                content=[ResponseOutputText(annotations=[], text=text, type="output_text")],
            )
        )
    if turn.tool_use is not None:
        name = str(turn.tool_use.name).removeprefix(MCP_TOOL_PREFIX)
        if name not in known_tools:
            logger.warning("model called unknown tool %r; Strix will report it", name)
        output.append(
            ResponseFunctionToolCall(
                id=f"fc_{turn.tool_use.id}",
                call_id=turn.tool_use.id,
                name=name,
                arguments=json.dumps(turn.tool_use.input, ensure_ascii=False),
                type="function_call",
                status="completed",
            )
        )
    return output


class ClaudeCodeModel(Model):
    """One headless Claude Code run per turn, used as a completion endpoint."""

    def __init__(self, model: str, *, reasoning_effort: str | None = None) -> None:
        self.model = model
        self._effort = reasoning_effort

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],  # noqa: A002
        model_settings: ModelSettings,  # noqa: ARG002
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,  # noqa: ARG002
        handoffs: list[Handoff],  # noqa: ARG002
        tracing: ModelTracing,  # noqa: ARG002
        *,
        previous_response_id: str | None = None,  # noqa: ARG002
        conversation_id: str | None = None,  # noqa: ARG002
        prompt: ResponsePromptParam | None = None,  # noqa: ARG002
    ) -> ModelResponse:
        try:
            options = _build_options(
                self.model, system_instructions, _function_tools(tools), self._effort
            )
        except ImportError as exc:
            raise ClaudeCodeError(
                "claude-code/ models need the Claude Agent SDK: pip install claude-agent-sdk"
            ) from exc

        from strix.config import load_settings  # noqa: PLC0415

        timeout = load_settings().llm.claude_code_timeout
        async with _concurrency_limit():
            started = time.monotonic()
            try:
                turn = await asyncio.wait_for(
                    _run_turn(build_content_blocks(input), options), timeout=timeout
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Claude Code did not answer within {timeout:.0f}s (STRIX_CLAUDE_CODE_TIMEOUT)"
                ) from exc
            logger.debug("claude-code turn took %.1fs", time.monotonic() - started)

        known = {t.name for t in tools if isinstance(t, FunctionTool)}
        usage = _usage_from((turn.result.usage if turn.result else None) or turn.last_usage)
        return ModelResponse(output=_to_output(turn, known), usage=usage, response_id=None)

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],  # noqa: A002
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: ResponsePromptParam | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Serve the SDK's streamed run loop from one completed response."""
        response = await self.get_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )
        usage = response.usage
        yield ResponseCompletedEvent(
            type="response.completed",
            sequence_number=0,
            response=Response(
                id=response.response_id or "resp_claude_code",
                created_at=time.time(),
                model=self.model,
                object="response",
                output=list(response.output),
                tool_choice="auto",
                tools=[],
                parallel_tool_calls=False,
                usage=ResponseUsage(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    input_tokens_details=usage.input_tokens_details,
                    output_tokens_details=usage.output_tokens_details,
                ),
            ),
        )


__all__ = [
    "ALIAS_CONTEXT_LIMITS",
    "BILLING_RISK_ENV_VARS",
    "PREFIX",
    "ClaudeCodeError",
    "ClaudeCodeModel",
    "ClaudeCodeRefusalError",
    "billing_risk_env_vars",
    "build_content_blocks",
    "check_available",
    "claude_code_model",
]
