"""WorkBuddy adapter -- runs the bundled CodeBuddy CLI headlessly."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

ALLOWED = "WebSearch WebFetch"
BLOCKED = ("Bash Write Edit NotebookEdit Read Glob Grep "
           "Task TodoWrite KillShell BashOutput")

MAX_TURNS = 60
TIMEOUT_SECONDS = 420
MAX_CONCURRENCY = 4


class AgentFailure(Exception):
    """A failure the CLI reported in-band, already phrased for the user."""


def _node() -> str | None:
    """A Node runtime: PATH first, then the managed one WorkBuddy ships."""
    node = shutil.which("node")
    if node:
        return node
    versions = sorted(
        Path.home().glob(".workbuddy/binaries/node/versions/*/node.exe"))
    return str(versions[-1]) if versions else None


def _bundled_cli() -> str | None:
    """The CLI entry script inside the WorkBuddy desktop install."""
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        return None
    cand = (Path(root) / "Programs" / "WorkBuddyAI" / "resources"
            / "app.asar.unpacked" / "cli" / "bin" / "codebuddy")
    return str(cand) if cand.exists() else None


def _command() -> list[str] | None:
    """How to invoke the CLI on this machine, or None when it cannot be."""
    exe = shutil.which("codebuddy") or shutil.which("cbc")
    if exe:
        return [exe]
    cli, node = _bundled_cli(), _node()
    if cli and node:
        return [node, cli]
    return None


def available() -> tuple[bool, str]:
    """Whether the CodeBuddy CLI can be invoked, and why not when it cannot."""
    if _command() is not None:
        return True, ""
    return False, ("The CodeBuddy CLI was not found on PATH and no bundled "
                   "copy was located (looked under the WorkBuddy desktop "
                   "install and the managed Node runtime). Install/sign in to "
                   "WorkBuddy, or choose the Claude SDK agent instead.")


def needs_sign_in(message: str | None) -> bool:
    """Part of the adapter contract, so the UI can ask either backend the same
    question. Always False here: this CLI's sign-in lives inside the WorkBuddy
    desktop app, so there is no `claude`-style command to point the user at --
    `available()` already says what to do when it is missing."""
    return False


def _free_port() -> int:
    """A port nothing is listening on, for the CLI's framework HTTP server."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(prompt: str, system: str, model: str | None, timeout: int) -> str:
    cmd = _command()
    if cmd is None:
        raise AgentFailure(available()[1])
    args = cmd + [
        "-p", "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--max-turns", str(MAX_TURNS),
        "--allowedTools", ALLOWED,
        "--disallowedTools", BLOCKED,
        "--setting-sources", "",
    ]
    if system:
        args += ["--append-system-prompt", system]
    if model:
        args += ["--model", model]
    args.append(prompt)

    env = {**os.environ, "SERVER__PORT": str(_free_port())}
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8",
                              errors="replace", env=env,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise AgentFailure(f"Timed out after {timeout}s.") from None

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = err or out or f"exit code {proc.returncode}"
        raise AgentFailure(f"CodeBuddy CLI failed: {detail[:400]}")

    try:
        events = json.loads(out)
    except json.JSONDecodeError:
        return out
    if not isinstance(events, list):
        events = [events]
    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == "result":
            if event.get("is_error"):
                raise AgentFailure(
                    str(event.get("result")
                        or "CodeBuddy reported an error."))
            result = event.get("result")
            if isinstance(result, str) and result.strip():
                return result.strip()
    for event in reversed(events):
        if not (isinstance(event, dict) and event.get("type") == "message"
                and event.get("role") == "assistant"):
            continue
        content = event.get("content")
        if isinstance(content, list):
            text = "".join(str(b.get("text", "")) for b in content
                           if isinstance(b, dict)
                           and b.get("type") in ("output_text", "text")).strip()
            if text:
                return text
    return out


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

    def one(p: str) -> tuple[str, str | None]:
        try:
            return _run(p, system, model, timeout), None
        except AgentFailure as exc:
            return "", str(exc)
        except Exception as exc:                    # noqa: BLE001
            return "", f"{type(exc).__name__}: {exc}"

    results: list[tuple[str, str | None]] = [("", "")] * len(prompts)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(one, p): i for i, p in enumerate(prompts)}
        pending: set = set(futures)
        reported = 0
        while pending:
            done, pending = wait(pending, timeout=0.5,
                                 return_when=FIRST_COMPLETED)
            for f in done:
                results[futures[f]] = f.result()
            if on_progress and len(futures) - len(pending) != reported:
                reported = len(futures) - len(pending)
                on_progress(reported, len(prompts))
    return results


def research(prompt: str, system: str = "", model: str | None = None,
             timeout: int = TIMEOUT_SECONDS) -> tuple[str, str | None]:
    """Run one research task. Returns `(answer, error)`; never raises."""
    return research_many([prompt], system, model, timeout, concurrency=1)[0]
