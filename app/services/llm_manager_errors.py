"""Typed error contract for the LLMManager (Chat pipeline refactor).

The exception a caller sees is part of the module's public contract (Gate 2).
Each type subclasses the builtin it logically is, so existing `except
ValueError`/`except LookupError` handlers and tests keep working, while callers
that want to branch on the specific failure can.

Spec: docs/SPEC_LLM_MANAGER.md §10 (error contracts).
"""
from __future__ import annotations


class LLMManagerError(Exception):
    """Base for every error raised by the LLMManager surface."""


class TurnIdRequiredError(LLMManagerError, ValueError):
    """`call()` was invoked without a non-empty turn_id (DEP-1). Fail-loud so the
    Attribution rule is enforced at the call boundary, never silently NULL."""


class PromptNotFoundError(LLMManagerError, LookupError):
    """No active prompt template resolved for a module_key (no matching variant
    and no 'default'). A configuration/seed error, not a transient one."""


class LLMCallError(LLMManagerError):
    """The provider call failed terminally — after retries (≤2, backoff), 429
    handling, and model fallback all exhausted. Carries the last underlying
    error for diagnostics."""

    def __init__(self, message: str, *, module_key: str | None = None, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.module_key = module_key
        self.__cause__ = cause
