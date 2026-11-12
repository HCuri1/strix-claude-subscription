<div align="center">

# Strix — Claude Subscription Edition

### A modified copy of [Strix](https://github.com/usestrix/strix) (built from the 1.7.0 release source) that runs on your Claude Pro/Max subscription instead of metered API billing.

<a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-3b82f6?style=flat-square" alt="License"></a>
<a href="https://github.com/usestrix/strix"><img src="https://img.shields.io/badge/Upstream-usestrix%2Fstrix-2b9246?style=flat-square" alt="Upstream project"></a>
<a href="https://docs.strix.ai"><img src="https://img.shields.io/badge/Docs-docs.strix.ai-555555?style=flat-square" alt="Docs"></a>

</div>

---

> [!IMPORTANT]
> **This is an unofficial, independent project.** It is built from a downloaded copy of the Strix `1.7.0` release source (not a GitHub fork — there's no fork relationship on GitHub itself, though its git history is now relinked to upstream's real `v1.7.0` tag, see [UPDATING.md](UPDATING.md)) and is not affiliated with, endorsed by, or supported by the Strix team or `usestrix`. For the official project, support, and the full feature set (Strix Cloud, Enterprise, managed platform), go to **[usestrix/strix](https://github.com/usestrix/strix)** and **[strix.ai](https://strix.ai)**.

## What this is

[Strix](https://github.com/usestrix/strix) is an open-source, multi-agent AI pentesting tool: autonomous agents that run your code, find vulnerabilities, and validate them with real proof-of-concept exploits. The original project is LLM-agnostic through LiteLLM and bills per token against whatever provider key you give it.

This project adds one thing on top of Strix `1.7.0`: a **`claude-code/<model>` model backend** that routes every LLM call Strix makes through your own locally-installed, already-authenticated [`claude`](https://code.claude.com) binary (via the official [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)) instead of a per-token API key. If you already pay for Claude Pro or Max, scans run on that subscription at **$0 additional API cost**.

Everything else — the agent graph, the sandbox, the tools, the reporting — is untouched from that release. See **[CHANGES.md](CHANGES.md)** for the exact list of modified files and what each change does, and **[UPDATING.md](UPDATING.md)** for how this repo picks up later upstream releases.

### How it works, briefly

- Strix still owns the whole agent loop (tool execution, sandboxing, sessions, budgets, compaction). The model only answers one turn at a time.
- Each turn spawns one headless `claude` run, configured as a plain completion endpoint: Claude Code's own built-in tools are disabled, and Strix's tools are exposed to it as native tool schemas instead.
- Authentication is entirely Anthropic's: you run `claude auth login` once, and Strix never reads, stores, or forwards your OAuth session. It only ever shells out to the unmodified `claude` binary.
- History is replayed as a stable, append-only prompt each turn, so Claude's own prompt caching keeps working across the scan.

**Known limitations of this backend** (plain Strix + a metered key doesn't have these): images returned by tools are dropped rather than sent to the model; conversation history is flattened to text instead of native tool-call blocks; there is no token-level streaming (a turn returns whole). None of this affects correctness, only a bit of fidelity and latency per turn.

## Install

**Prerequisites:** Docker (running), [uv](https://docs.astral.sh/uv/), and the [Claude Code CLI](https://code.claude.com) signed in to a Claude Pro/Max account (`claude auth login`).

```bash
git clone https://github.com/HCuri1/strix-claude-subscription.git
cd strix-claude-subscription
uv sync
```

Run it straight from the source tree with `uv run`:

```bash
export STRIX_LLM="claude-code/sonnet"
uv run strix -n -t ./app-directory
```

> [!TIP]
> If you also have the official `strix` binary installed (`curl -sSL https://strix.ai/install | bash`), keep both without conflict by aliasing this version instead of overwriting it:
> ```bash
> echo "alias strix-dev='uv run --project $(pwd) strix'" >> ~/.zshrc   # or ~/.bashrc
> ```

### Interactive TUI

The interactive terminal UI is a separate Go ("Bubble Tea") sidecar. Running from source compiles it on first launch if you have Go installed:

```bash
# user-level install, no sudo needed
curl -LO https://go.dev/dl/go1.27.4.linux-amd64.tar.gz   # check go.dev/dl for your OS/arch
tar -C ~/.local -xzf go1.27.4.linux-amd64.tar.gz
echo 'export PATH=$HOME/.local/go/bin:$PATH' >> ~/.zshrc
```

Without Go, headless mode (`-n`, shown above) still works fully — it never starts the TUI.

## Configure: Claude subscription models

```bash
claude auth login                    # one-time, opens Anthropic's own sign-in flow
export STRIX_LLM="claude-code/sonnet"
claude auth status                   # confirm the active account any time
```

`claude-code/<model>` accepts either a short alias or a full versioned model id:

| Form | Example | Notes |
|---|---|---|
| Alias | `claude-code/sonnet`, `claude-code/opus`, `claude-code/haiku` | Always resolves to whatever Claude's CLI currently points that alias at |
| Full id | `claude-code/claude-opus-4-7`, `claude-code/claude-sonnet-5-5` | Pin an exact version — useful if a future alias update changes behavior you've validated for pentesting |

Optional tuning (same `~/.strix/cli-config.json` persistence as every other Strix setting):

```bash
export STRIX_CLAUDE_CODE_CONCURRENCY=3   # max parallel `claude` processes (default 3)
export STRIX_CLAUDE_CODE_TIMEOUT=600     # per-turn timeout in seconds (default 600)
```

### Reasoning effort

`STRIX_REASONING_EFFORT` controls how much the model "thinks" per turn, independent of which model you pick. Default is `medium`. Valid values: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` (not every provider supports every level — unsupported values fall back to the closest one it does).

```bash
export STRIX_REASONING_EFFORT=high   # more thorough per turn, burns through your subscription's usage cap faster
export STRIX_REASONING_EFFORT=low    # faster, cheaper turns — good default if you're hitting usage limits often
```

If you're running Strix scans and chatting with Claude Code side by side, remember they draw from the **same** subscription usage window — a lower effort here leaves more of that cap for everything else.

> [!WARNING]
> **Billing-risk check.** If `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`, or `CLAUDE_CODE_USE_VERTEX` are set in your shell, the `claude` binary will use them instead of your subscription login — real metered billing, while Strix still reports `$0`. Strix warns you at startup if it detects any of these; it never modifies your environment, so unset them yourself if you want the subscription route guaranteed.

Everything else about Strix — targets, scan modes, custom instructions, MCP servers, the local viewer, other LLM providers — works exactly as in the original release. See **[docs.strix.ai](https://docs.strix.ai)** for the full reference, and the original README/docs for Strix Cloud, Enterprise, and CI/CD integrations, which this project doesn't change.

> [!WARNING]
> **Authorized use only.** Strix actively tests the targets you point it at, so only run it against systems you own or have **explicit, written permission** to test, and stay within the agreed scope. Unauthorized testing is illegal in most jurisdictions. You alone are responsible for obtaining authorization and complying with the law. Provided "as is" with no warranty or liability for misuse.

## Credits & license

All credit for Strix itself — the agent architecture, the sandbox, the pentesting tools, the report pipeline, and everything not described above — goes to the original authors at **[usestrix/strix](https://github.com/usestrix/strix)**. Go support them, star the original repo, and use their [official releases](https://strix.ai) or [managed platform](https://app.strix.ai) if you want the full product and support behind it.

The Claude-subscription backend in this project was built by **Henrique Curi** ([@HCuri1](https://github.com/HCuri1)), on top of the official, unmodified [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).

Licensed under [Apache License 2.0](LICENSE), same as upstream. This is a modified version of the original source, as required by that license — see [CHANGES.md](CHANGES.md) for specifics.
