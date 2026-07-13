"""SSE round runner package: ``run_round`` (the per-discussion async
generator that drives one persona-by-persona turn loop) plus
``_ask_persona`` (the per-persona LLM streaming wrapper) and the two
stateful helpers they own — ``_ThinkBlockFilter`` (drops reasoning-model
``<think>...</think>`` blocks from the SSE feed) and ``TurnEvent``
(the dataclass wrapper for each yielded event).

Originally extracted from ``services.discussion_service`` as the C3-1 γ
slice in ``misty-mixing-harbor.md``; split from a single 1114-line
``round_runner.py`` module into this package (R6 PR1, pure move — no
behaviour change):

  * ``turn_exec``  — the per-turn execution primitives:
    ``_ThinkBlockFilter`` / ``TurnEvent`` / ``_PERSONA_TOOL_USAGE_HINT``
    / ``_ask_persona``.
  * ``loop``       — ``run_round``, the per-round SSE orchestrator.
  * ``followup``   — ``interject_followup``, the post-conclusion
    bounded 追問 Q&A (B4).

The import path ``services.discussion.round_runner`` is unchanged
(module → package), so ``services.discussion_service``'s back-compat
re-export block and every call site keep working untouched.

Imports are layered the same way the other ``services/discussion/``
modules do it:

  * Top-level: the already-extracted helper modules
    (``discussion.persona_config``, ``discussion.prompts``,
    ``discussion.transcript_format``, ``discussion.turn_parsing``,
    ``discussion.symbols``).
  * Lazy (function-local) from ``services.discussion_service``:
    ``STATUS_DRAFT`` / ``STATUS_RUNNING``, ``gather_market_context``,
    ``get_turns``, ``_upsert_round_context``, ``stream_chat``. These
    stay in ``discussion_service`` proper so the ~38 test sites that
    do ``patch("services.discussion_service.stream_chat", ...)`` (and
    similar) continue to land on the binding the running code reads.
    Same pattern α (synthesiser) and β (context_assembly) used.
"""
from services.discussion.round_runner.followup import (  # noqa: F401
    interject_followup,
)
from services.discussion.round_runner.loop import run_round  # noqa: F401
from services.discussion.round_runner.turn_exec import (  # noqa: F401
    _PERSONA_TOOL_USAGE_HINT,
    TurnEvent,
    _ask_persona,
    _ThinkBlockFilter,
)
