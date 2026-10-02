"""Claude Agent SDK adapter -- runs Claude Code as a library, not the API."""

from __future__ import annotations

import asyncio
from typing import Callable

ALLOWED = ["WebSearch", "WebFetch"]
BLOCKED = ["Bash", "Write", "Edit", "NotebookEdit", "Read", "Glob", "Grep",
           "Task", "TodoWrite", "KillShell", "BashOutput"]

MAX_TURNS = 60

TIMEOUT_SECONDS = 420

MAX_CONCURRENCY = 4


SIGN_IN_MESSAGE = (
    "The Claude Code CLI is not signed in — its OAuth session has expired and "
    "there is no refresh token left to renew it. Open a terminal, run "
    "`claude`, sign in, then come back and search again. Retrying before that "
    "will fail the same way on every batch, whichever model is selected."
)


def _version(path) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in path.parent.name.split("."))
    except ValueError:
        return ()


def cli_path() -> str | None:
    """The Claude Code binary to run, or None for the SDK's own choice.

    `CLAUDE_CODE_CLI` in `.env` wins. Otherwise the newest copy the Claude
    desktop app keeps under %APPDATA%\\Claude\\claude-code\\<version>\\: the SDK
    otherwise runs whatever `claude` is first on PATH, and an old one refuses
    new models outright ("version 2.1.280 or newer is required" for Opus 5.5)."""
    import os
    from pathlib import Path

    explicit = os.environ.get("CLAUDE_CODE_CLI", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    root = os.environ.get("APPDATA")
    if not root:
        return None
    found = [p for p in (Path(root) / "Claude" / "claude-code").glob("*/claude.exe")
             if _version(p)]
    return str(max(found, key=_version)) if found else None


def needs_sign_in(message: str | None) -> bool:
    """Is this failure the one a sign-in actually fixes?"""
    return bool(message) and SIGN_IN_MESSAGE in message


def available() -> tuple[bool, str]:
    """Whether a research run can start, and why not when it cannot."""
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False, ("claude-agent-sdk is not installed. "
                       "Run: pip install claude-agent-sdk")
    return True, ""


def _explain(said: str) -> str:
    """A CLI error message, shortened and, where there is a known fix, said."""
    if not said:
        return ""
    if "claude_code_version_too_old" in said or "does not support this model" in said:
        return ("This model needs a newer Claude Code than the one the app found. Update "
                "the Claude desktop app or run `claude update`, or point CLAUDE_CODE_CLI in "
                ".env at a newer claude.exe.")
    return f"Claude Code reported: {said[:300]}"


class AgentFailure(Exception):
    """A failure the CLI reported in-band, already phrased for the user."""


FAILURES = {
    "authentication_failed": SIGN_IN_MESSAGE,
    "oauth_token_expired": SIGN_IN_MESSAGE,
}


async def _run(prompt: str, system: str, model: str | None,
               effort: str | None = None, schema: dict | None = None,
               meta: dict | None = None) -> str:
    """One agent run. With `schema`, the CLI validates the final answer against
    it and hands it back as `structured_output`, returned here as JSON text so
    every caller parses one shape. `meta`, when given, collects the run's cost,
    turns and duration."""
    import json

    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  ResultMessage, TextBlock, query)

    options = ClaudeAgentOptions(
        system_prompt=system,
        allowed_tools=ALLOWED,
        disallowed_tools=BLOCKED,
        permission_mode="bypassPermissions",
        setting_sources=[],
        max_turns=MAX_TURNS,
        **({"cli_path": cli} if (cli := cli_path()) else {}),
        **({"model": model} if model else {}),
        **({"effort": effort} if effort else {}),
        **({"output_format": {"type": "json_schema", "schema": schema}} if schema else {}),
    )

    answer, failure = "", [""]

    def note(reason: str) -> None:
        if not failure[0]:
            failure[0] = reason

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                code = getattr(message, "error", None)
                if code:
                    # The CLI's own words where it gave any: an "unknown" code
                    # carries the real reason (an unsupported model, a 400) in
                    # the message text, which "reported: unknown" threw away.
                    said = "".join(b.text for b in message.content
                                   if isinstance(b, TextBlock)).strip()
                    note(FAILURES.get(code) or _explain(said)
                         or f"Claude Code reported: {code}.")
                    continue
                text = "".join(b.text for b in message.content
                               if isinstance(b, TextBlock)).strip()
                if text:
                    answer = text
            elif isinstance(message, ResultMessage):
                if getattr(message, "is_error", False):
                    note("Claude Code ended with an error result.")
                result = getattr(message, "result", None)
                if isinstance(result, str) and result.strip():
                    answer = result.strip()
                structured = getattr(message, "structured_output", None)
                if structured is not None:
                    answer = json.dumps(structured)
                if meta is not None:
                    meta.update(cost_usd=getattr(message, "total_cost_usd", None),
                                turns=getattr(message, "num_turns", None),
                                seconds=(getattr(message, "duration_ms", 0) or 0) / 1000)
    except Exception:                               # noqa: BLE001
        if failure[0]:
            raise AgentFailure(failure[0]) from None
        raise

    if failure[0]:
        raise AgentFailure(failure[0])
    return answer


def research_many(prompts: list[str], system: str = "",
                  model: str | None = None,
                  timeout: int = TIMEOUT_SECONDS,
                  concurrency: int = MAX_CONCURRENCY,
                  on_progress: Callable[[int, int], None] | None = None
                  ) -> list[tuple[str, str | None]]:
    """Run several research tasks at once. One `(answer, error)` per prompt."""
    ok, why = available()
    if not ok:
        return [("", why) for _ in prompts]
    if not prompts:
        return []

    async def gather() -> list[tuple[str, str | None]]:
        limit = asyncio.Semaphore(max(1, concurrency))

        async def one(p: str) -> tuple[str, str | None]:
            async with limit:
                try:
                    return await asyncio.wait_for(
                        _run(p, system, model), timeout=timeout), None
                except asyncio.TimeoutError:
                    return "", f"Timed out after {timeout}s."
                except AgentFailure as exc:
                    return "", str(exc)
                except Exception as exc:            # noqa: BLE001
                    return "", f"{type(exc).__name__}: {exc}"

        tasks = [asyncio.create_task(one(p)) for p in prompts]
        reported = 0
        while tasks:
            _, pending = await asyncio.wait(tasks, timeout=0.5,
                                            return_when=asyncio.FIRST_COMPLETED)
            done_n = len(tasks) - len(pending)
            if on_progress and done_n != reported:
                reported = done_n
                on_progress(done_n, len(tasks))
            if not pending:
                break
        return [t.result() for t in tasks]

    try:
        return asyncio.run(gather())
    except Exception as exc:                        # noqa: BLE001
        return [("", f"{type(exc).__name__}: {exc}") for _ in prompts]


def research_structured(prompt: str, system: str, schema: dict,
                        model: str | None = None, effort: str | None = None,
                        timeout: int = TIMEOUT_SECONDS
                        ) -> tuple[str, str | None, dict]:
    """One research task whose answer must match `schema`.

    Returns `(answer_json, error, meta)`; never raises. `meta` carries the
    run's cost in USD, its turns and its wall time when the CLI reports them."""
    ok, why = available()
    if not ok:
        return "", why, {}
    meta: dict = {}
    try:
        answer = asyncio.run(asyncio.wait_for(
            _run(prompt, system, model, effort=effort, schema=schema, meta=meta),
            timeout=timeout))
        return answer, None, meta
    except asyncio.TimeoutError:
        return "", f"The research agent did not finish within {timeout // 60} minutes.", meta
    except AgentFailure as exc:
        return "", str(exc), meta
    except Exception as exc:                        # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}", meta


def research(prompt: str, system: str = "", model: str | None = None,
             timeout: int = TIMEOUT_SECONDS) -> tuple[str, str | None]:
    """Run one research task. Returns `(answer, error)`; never raises."""
    ok, why = available()
    if not ok:
        return "", why
    try:
        return asyncio.run(asyncio.wait_for(
            _run(prompt, system, model), timeout=timeout)), None
    except asyncio.TimeoutError:
        return "", f"The research agent did not finish within {timeout}s."
    except AgentFailure as exc:
        return "", str(exc)                 # already phrased for the user
    except Exception as exc:                        # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"
