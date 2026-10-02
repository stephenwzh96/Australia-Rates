"""Research-agent panels and the controls they share.

The agent picker, sign-in handling and retry controls used by every billable
panel: the release consensus search, the speech roundup and the street call
book. Every run happens only on a button."""

from __future__ import annotations

import time
from datetime import date, datetime

import streamlit as st

from data import agent as claude_agent
from ui import components as C
from ui import sources as src

AGENTS = src.RESEARCH_AGENTS
LABELS = src.RESEARCH_AGENT_LABELS
TIMEOUTS = (7, 10, 15, 20, 30)
CACHE_TTL = 3600


def _reset_model() -> None:
    st.session_state["speech_model_override"] = src.RESEARCH_AGENT_MODELS.get(
        st.session_state.get("speech_backend", src.DEFAULT_RESEARCH_BACKEND), "")


def agent_picker(slots=None) -> tuple[str, str]:
    """Backend + model, bound to one pair of session keys shared app-wide.

    `slots` are two columns of the caller's own row, so the two dropdowns
    sit beside the caller's other dropdowns instead of in a row of their own."""
    st.session_state.setdefault("speech_backend", src.DEFAULT_RESEARCH_BACKEND)
    a1, a2 = slots if slots is not None else st.columns([1.2, 1.6])
    # In a caller's bar the two dropdowns carry their own widths.
    w_agent, w_model = (150, 175) if slots is not None else ("stretch", "stretch")
    with a1:
        backend = st.selectbox(
            "Research agent", tuple(AGENTS), format_func=LABELS.get, width=w_agent,
            key="speech_backend", on_change=_reset_model,
            help="One setting for the whole app: the speech roundup, bank calls and "
                 "release consensus all run on it. Answers are cached per agent.")
    st.session_state.setdefault("speech_model_override", src.RESEARCH_AGENT_MODELS.get(backend, ""))
    choices = src.RESEARCH_AGENT_MODEL_CHOICES.get(backend, [src.RESEARCH_AGENT_MODELS.get(backend, "")])
    if st.session_state["speech_model_override"] not in choices:
        st.session_state["speech_model_override"] = choices[0]
    with a2:
        model = st.selectbox("Model", choices, key="speech_model_override", width=w_model,
                             help="Passed to the agent CLI's --model flag.")
    return backend, (model or "").strip()


def needs_sign_in(backend: str, message: str | None) -> bool:
    ask = getattr(AGENTS[backend], "needs_sign_in", None)
    return bool(ask and ask(message))


def agent_blocked(backend: str, why: str) -> None:
    if needs_sign_in(backend, why):
        st.error(why)
        st.code("claude", language="bash")
        C.note("Sign in, then search again — the CLI reports sign-in state when a search runs.")
    else:
        st.info(why)


def _stamp(entry: dict, fallback: str) -> str:
    try:
        return datetime.fromisoformat(entry["stamp"]).strftime("%d %b %Y, %H:%M")
    except (KeyError, ValueError):
        return fallback


def _agent_tail(entry: dict) -> str:
    tail = f" · {entry['meeting']}" if entry.get("meeting") else ""
    tail += f" · {LABELS.get(entry.get('backend', 'claude'), '?')}"
    return tail + (f" · {entry['model']}" if entry.get("model") else "")


def _run_timeout(key: str, width="stretch") -> int:
    st.session_state.setdefault(key, 30)
    return st.selectbox("Search timeout", TIMEOUTS, format_func=lambda m: f"{m} min", key=key,
                        width=width,
                        help="Per-batch budget. Batches run in parallel, so the wait is "
                             "roughly the slowest batch. A timed-out batch can be retried.")


def _retry_controls(backend: str, error: str | None, prefix: str, retry_n: int,
                    key_for) -> bool:
    """Sign-in or retry prompt after a failed/partial run. Returns True to stop."""
    if needs_sign_in(backend, error):
        agent_blocked(backend, claude_agent.SIGN_IN_MESSAGE)
        if st.button("I've signed in — search again", type="primary", key=f"{prefix}_signin"):
            st.session_state[f"{prefix}_retry"] = retry_n + 1
            st.session_state[f"{prefix}_key"] = key_for(retry_n + 1)
            st.rerun()
        return True
    st.warning("Some searches failed or timed out. Retrying re-asks everything; "
               "failed batches are never served from the cache.")
    if st.button("Retry failed searches", key=f"{prefix}_retry_btn", type="primary"):
        st.session_state[f"{prefix}_retry"] = retry_n + 1
        st.session_state[f"{prefix}_key"] = key_for(retry_n + 1)
        st.rerun()
    return False
