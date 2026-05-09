"""Transcript / history formatters for discussion prompts.

Three pure functions that turn ``DiscussionTurn`` rows into the
prompt-ready strings the persona / synthesizer LLM calls consume:

  - ``_summarize_turn_content`` — single-line gist of one turn's
    content, used in the older-turns summary block of a persona
    prompt's "## 先前發言" section.
  - ``_format_history`` — two-tier compression. The N most recent
    turns appear verbatim (live debate the persona is reacting to),
    older turns up to ``_MAX_HISTORY_TURNS`` appear as one-line
    summaries. Recency bias keeps the freshest turn at the bottom.
  - ``_format_transcript`` — full transcript for the synthesizer.
    User-input turns get a different prefix so the synthesizer
    doesn't try to *answer* a post-mortem self-critique question
    in the ``reasoning`` field.

Pure (no DB, no LLM, no I/O). The only external dependency is the
``USER_PERSONA_ID`` constant which still lives in
``discussion_service`` because too many other call sites reach for
it; we import it lazily inside the formatters to avoid a circular
import at module load.
"""
from __future__ import annotations

from models.discussion import DiscussionTurn

# How many recent turns appear verbatim vs as one-line summaries.
# `_MAX_HISTORY_TURNS` caps the total window because Chinese-prompt
# token cost scales fast with turn count (~300 chars × 3 BPE = ~900
# tokens per verbatim turn; 8 verbatim + 22 summarised lands at ~10K
# input tokens before the actual `## 你的角色` block, which is the
# remaining budget cap).
_MAX_HISTORY_TURNS = 30
_FULL_HISTORY_TURNS = 8
_HISTORY_SUMMARY_CHARS = 120


def _summarize_turn_content(content: str) -> str:
    """Compress a turn's full content down to one line for the older-
    history block. Strips markdown emphasis + collapses whitespace +
    truncates at `_HISTORY_SUMMARY_CHARS`. The persona doesn't need
    the full text from 4 rounds ago — only the gist of the speaker's
    previous position so they can spot drift / contradictions."""
    body = (content or "").strip()
    if not body:
        return "（同意，無補充）"
    # Drop markdown bold / italic markers + bullet hyphens.
    body = body.replace("**", "").replace("__", "")
    # Collapse whitespace + newlines.
    body = " ".join(body.split())
    if len(body) > _HISTORY_SUMMARY_CHARS:
        body = body[:_HISTORY_SUMMARY_CHARS] + "…"
    return body


def _format_history(prior_turns: list[DiscussionTurn]) -> str:
    """Build the `## 先前發言` block for a persona prompt.

    Two-tier compression keeps the prompt budget bounded:
      - The N most recent turns (`_FULL_HISTORY_TURNS`) appear in
        full — these are the live debate the persona is reacting to.
      - Older turns up to `_MAX_HISTORY_TURNS` appear as a single-
        line summary so the persona retains continuity ("buffett 第
        1 輪看好 2330, 我此輪也補強") without paying for verbatim
        text from rounds ago.

    The full window comes after the summary block so the LLM's
    recency bias works in our favour — the most recent turn is the
    last thing in the prompt before its own "你現在的任務" line.
    """
    if not prior_turns:
        return "（你是本場第一位發言者）"
    from services.discussion_service import USER_PERSONA_ID

    window = prior_turns[-_MAX_HISTORY_TURNS:]
    if len(window) <= _FULL_HISTORY_TURNS:
        recent = window
        older: list[DiscussionTurn] = []
    else:
        split = len(window) - _FULL_HISTORY_TURNS
        older = window[:split]
        recent = window[split:]

    def _render(t: DiscussionTurn, body: str) -> str:
        # User injections aren't analyst opinions — render them as a
        # directive from the discussion's owner so personas know the
        # next round must respond to it. Keeps the same `第N輪` prefix
        # for ordering / recency cues.
        if t.persona_id == USER_PERSONA_ID:
            return f"- 第{t.round}輪 · 【討論發起人插話】：{body}"
        return f"- 第{t.round}輪 · {t.persona_id} · {t.stance}：{body}"

    sections: list[str] = []
    if older:
        sections.append("（較早輪次摘要）")
        for t in older:
            sections.append(_render(t, _summarize_turn_content(t.content)))
    if older and recent:
        sections.append("")
        sections.append("（最近發言全文）")
    for t in recent:
        body = t.content.strip() or "（同意，無補充）"
        sections.append(_render(t, body))
    return "\n".join(sections)


def _format_transcript(turns: list[DiscussionTurn]) -> str:
    """Render the full transcript for the synthesizer prompt.

    Persona turns get a `[第N輪/persona_id/stance]` prefix.
    User-input turns (e.g. post-mortem self-critique prompts
    injected via `inject_user_message`) are rendered as a clearly
    labelled directive — without this distinction the synthesizer
    LLM treats `_user` as just another analyst and may try to
    *answer* the directive's questions in the `reasoning` field
    instead of synthesizing the discussion. The longer post-mortem
    prompt (4 structured questions) makes that failure mode worse:
    the LLM dumps a long answer + runs out of `max_tokens` mid-JSON
    and the response fails to parse.
    """
    if not turns:
        return "（無發言）"
    from services.discussion_service import USER_PERSONA_ID

    lines = []
    for t in turns:
        body = t.content.strip() or "（同意，無補充）"
        if t.persona_id == USER_PERSONA_ID:
            lines.append(
                f"[第{t.round}輪 · 討論發起人指示（請納入後續分析考量，"
                f"勿視為專家意見、勿直接回答其中的提問）]\n{body}"
            )
        else:
            lines.append(f"[第{t.round}輪/{t.persona_id}/{t.stance}] {body}")
    return "\n".join(lines)
