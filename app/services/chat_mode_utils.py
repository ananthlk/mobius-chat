"""Translation between chat's own ``chat_mode`` vocabulary (quick/copilot/
agentic/task) and RAG's ``caller_mode`` preset vocabulary (chat.default/
chat.copilot/chat.thinking).

2026-08-15 (Chat Master + Retriever, live-data finding): every SkillCall
dispatch site in react_loop.py sent raw ``chat_mode`` straight through as
RAG's ``caller_mode`` (see corpus_search.py's now-stale 2026-08-06 docstring
claiming that WAS the agreed vocabulary). RAG's ``CALLER_MODE_PRESETS``
(mobius-rag/app/services/corpus_search_router.py) only recognizes the
``chat.*`` keys — none of chat's raw strings ever matched, so every dispatch
silently fell through to ``chat.default``, and Retriever's ``chat.thinking``
16k-token upgrade never reached agentic turns. Two sides each assumed the
other owned translation; neither did. This module is the single canonical
place that now does.

NOT the same vocabulary problem control_vocab.py solves (that's the
LLMManager v2 autonomy/speed control-plane axis, SPEC_LLMMANAGER_V2.md §4) —
this is specifically the RAG corpus_search routing-preset boundary.
"""
from __future__ import annotations

# Ananth-confirmed mapping (2026-08-15). quick/task both land on chat.default
# today: quick has no distinct RAG preset yet (would need a new chat.quick
# preset on RAG's side to differentiate), and task mode was never addressed
# by either side's original vocabulary -- chat.default is the safe, already
# was the DE FACTO existing behavior.
_CHAT_MODE_TO_CALLER_MODE: dict[str, str] = {
    "agentic": "chat.thinking",
    "copilot": "chat.copilot",
    "quick": "chat.default",
    "task": "chat.default",
}

DEFAULT_CALLER_MODE = "chat.default"


def translate_chat_mode_to_caller_mode(chat_mode: str | None) -> str:
    """Map chat's ``chat_mode`` to the RAG ``caller_mode`` preset key.

    Unrecognized/missing input falls back to ``chat.default`` -- the same
    value RAG's own ``resolve_preferences()`` defaults to server-side, so an
    unmapped chat_mode degrades to today's existing (accidental) behavior
    rather than a new failure mode.
    """
    key = (chat_mode or "").strip().lower()
    return _CHAT_MODE_TO_CALLER_MODE.get(key, DEFAULT_CALLER_MODE)
