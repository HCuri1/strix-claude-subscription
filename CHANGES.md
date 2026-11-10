# Changes from the Strix `1.7.0` release source

This project is built from a downloaded copy of the [Strix](https://github.com/usestrix/strix)
`1.7.0` release source (not a GitHub fork). It adds one feature on top: a
`claude-code/<model>` LLM backend that routes model calls through the user's
own authenticated `claude` binary (Claude Agent SDK) instead of a metered API
key, so scans run on a Claude Pro/Max subscription at $0 additional API cost.
This file lists every file that was added or modified to do that, as required
by the [Apache License 2.0](LICENSE), §4(b).

Base version: Strix `1.7.0`.

## New files

- **`strix/llm/claude_code.py`** — the backend itself. Parses the
  `claude-code/<model>` prefix; spawns one headless `claude` run per model
  turn via the Claude Agent SDK; exposes Strix's tools to it as native tool
  schemas; maps its response back to the Agents SDK's response format; maps
  SDK errors (rate limits, sign-in failures, safety refusals) to errors Strix
  already knows how to handle or report clearly. Also provides the
  `ALIAS_CONTEXT_LIMITS` best-effort table and the `billing_risk_env_vars()`
  check described below.
- **`tests/test_claude_code_model.py`** — unit tests for the above.

## Modified files

### Wiring the new backend into model selection

- **`strix/config/models.py`** — `StrixProvider.get_model` gains a
  `claude-code/` branch (parallel to the existing ChatGPT-subscription
  branch) that builds a `ClaudeCodeModel`; `uses_chat_completions_tool_schema`
  and `routes_through_litellm` are taught about the new prefix;
  `configure_sdk_model_defaults` skips the LiteLLM/OpenAI-SDK setup that
  doesn't apply to this route, but still runs the LiteLLM setup a
  *different*, LiteLLM-routed dedupe model may need; `StrixProvider` gained a
  `reasoning_effort` constructor override so a dedicated dedupe model can use
  its own `STRIX_DEDUPE_REASONING_EFFORT` instead of silently inheriting the
  main model's.
- **`strix/config/codex.py`** — `auth_mode()` reports `"subscription"` (so
  the usage ledger reports $0 like the ChatGPT-subscription route already
  does) for `claude-code/` models too. Added `subscription_label()`, used
  everywhere the UI used to hardcode the string "ChatGPT subscription".
- **`strix/core/runner.py`** — skips the SDK API-route reconfiguration step
  for `claude-code/` models (it doesn't apply to this route).
- **`strix/llm/context_budget.py`** — `_model_info()` now skips the
  provider-prefixed LiteLLM lookup for `claude-code/` the same way it already
  did for `chatgpt/`, so a full versioned model id (e.g.
  `claude-code/claude-opus-4-7`) resolves its real context window/output
  limit instead of a generic, much smaller fallback. Bare aliases
  (`claude-code/sonnet` etc.), which have no LiteLLM entry at all, fall back
  to the best-effort `ALIAS_CONTEXT_LIMITS` table in `claude_code.py` before
  the configured generic fallback.
- **`strix/config/settings.py`** — added `claude_code_concurrency` /
  `claude_code_timeout` to `LlmSettings` (`STRIX_CLAUDE_CODE_CONCURRENCY` /
  `STRIX_CLAUDE_CODE_TIMEOUT`), replacing ad-hoc `os.environ` reads so these
  tunables are validated and persisted like every other Strix setting.
- **`strix/report/dedupe.py`** — `resolve_dedupe_model` passes a dedicated
  dedupe model's own reasoning effort through to `StrixProvider`.
- **`pyproject.toml`** — added `claude-agent-sdk>=0.2.180` as a dependency.

### User-facing correctness and safety

- **`strix/interface/environment.py`** — validates `claude-code/` models at
  startup the same way the ChatGPT-subscription route is validated (checks
  `claude auth status` instead of requiring `LLM_API_KEY`); warns (without
  blocking or modifying anything) if `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`, or
  `CLAUDE_CODE_USE_VERTEX` are set, since the Claude Agent SDK passes the
  parent environment through to the `claude` subprocess and any of these
  would silently switch it to metered billing.
- **`strix/interface/main.py`** — `_subscription_error_hint` and the model
  connection error panel recognize `claude-code/` sign-in failures and point
  at `claude auth login` / `claude auth status`, instead of only recognizing
  ChatGPT-subscription errors.
- **`strix/interface/utils.py`** — the CLI's live/final stats text uses
  `codex.subscription_label()` instead of the hardcoded string "ChatGPT
  subscription", so a Claude-subscription run is labeled correctly.
- **`strix/interface/scan_setup.py`**, **`strix/report/state.py`** — persist
  that same provider label into the run record, so a resumed run keeps
  showing the right label.
- **`strix/interface/tui/internal/app/view.go`**,
  **`strix/interface/tui/internal/app/model_test.go`** — same label fix in
  the Go interactive TUI.
- **`strix/interface/viewer/frontend/src/components/RunDetails.tsx`** — same
  label fix in the local web viewer.

### Tests extended for the above

`tests/test_context_budget.py`, `tests/test_models.py`,
`tests/test_dedupe_model.py`, `tests/test_main_launch.py`,
`tests/test_codex_auth.py`, `strix/interface/tui/internal/app/model_test.go`.

## Not changed

Everything else: the agent graph and prompts, the Docker sandbox, the
pentesting tools, the report/SARIF/PDF pipeline, every other LLM provider
route, Strix Cloud, CI/CD integrations, and the rest of the CLI. This project
is additive — removing `claude-code/` from `STRIX_LLM` makes it behave exactly
like upstream Strix `1.7.0`.
