"""Conclusion synthesizer + the three structured side-effects it runs
after the LLM call returns: PR-1 quality signals, PR-C2 confidence
calibration, PR-5c baseline-delta annotation.

Extracted from ``services.discussion_service`` (~640 LOC) as part of
the C3-1 split tracked in ``misty-mixing-harbor.md``. The orchestrator
in ``discussion_service.synthesize_conclusion`` was the single
heaviest function in that 2300-LOC file; lifting it out (and the
three helpers it dispatches on) cuts the service body by ~30 % while
keeping every public symbol available via the re-export shim at the
bottom of ``discussion_service.py``.

Imports stay layered the way the rest of the ``services/discussion/``
subfolder uses them:

  * Prompt templates + freshness preamble from ``discussion.prompts``
  * Transcript / history formatters from ``discussion.transcript_format``
  * ``_safe_conclusion`` + ``compute_conclusion_diff`` from
    ``discussion.conclusion_parsing``
  * ``extract_focus_symbols`` from ``discussion.symbols``
  * ``_extract_lessons_payload`` from ``discussion.lessons``

For the few CRUD entry-points still living in ``discussion_service``
(``get_turns``, ``get_round_contexts``, ``gather_market_context``,
the status / user-persona constants), we lazy-import them at call
time to keep the module-load graph acyclic — same pattern PR-B of
the tw_market_service split used for
``read_ohlcv_range_autosession``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.discussion import Discussion, DiscussionTurn
from services.discussion.ctx_minify import _minify_for_prompt
from services.discussion.conclusion_parsing import (
    _safe_conclusion,
    compute_conclusion_diff,
)
from services.discussion.lessons import _extract_lessons_payload
from services.discussion.prompts import (
    _SYNTHESIZER_SYSTEM,
    _SYNTHESIZER_USER_TEMPLATE,
    _format_freshness_preamble,
)
from services.discussion.symbols import extract_focus_symbols
from services.discussion.transcript_format import _format_transcript

log = logging.getLogger(__name__)


async def _apply_calibration_to_conclusion(
    db: AsyncSession,
    discussion: Discussion,
    conclusion: dict[str, Any],
) -> None:
    """PR-C2 follow-up: layer the strategy's isotonic calibration
    curve over the conclusion's ``recommendations``. In place;
    no-op when:

      - the discussion has no sweep parent
      - the parent sweep has no strategy_id
      - the strategy has no calibration_curve yet (cold-start)
      - the conclusion has no recommendations (parse error
        placeholder, unrecoverable LLM output)

    Each recommendation gains a ``calibrated_confidence`` sibling
    to the existing ``confidence`` field; the raw value is preserved
    so PR-C1's outcome_vector still records what the synthesizer
    originally emitted (needed for the next isotonic re-fit).
    """
    sweep_id = getattr(discussion, "sweep_id", None)
    if sweep_id is None:
        return
    recommendations = conclusion.get("recommendations") or []
    if not isinstance(recommendations, list) or not recommendations:
        return

    from models.backtest_sweep import BacktestSweep
    from models.discussion_strategy_template import (
        DiscussionStrategyTemplate,
    )
    sweep_row = await db.scalar(
        select(BacktestSweep).where(BacktestSweep.id == sweep_id),
    )
    if sweep_row is None or sweep_row.strategy_id is None:
        return
    tmpl = await db.scalar(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == sweep_row.strategy_id,
        )
    )
    if tmpl is None or not tmpl.calibration_curve:
        return

    from services.confidence_calibrator import apply_calibration
    for rec in recommendations:
        if not isinstance(rec, dict):
            continue
        try:
            raw = float(rec.get("confidence", 0.5))
        except (TypeError, ValueError):
            raw = 0.5
        # Audit follow-up #3: reject NaN / ±inf at the boundary so
        # downstream brier math and reliability buckets stay
        # finite. `apply_calibration`'s `min/max` clamp would
        # collapse NaN to a corner value (a NaN that snuck through
        # to confidence=1.0 would be misleading rather than
        # diagnostic), so neutralize to 0.5 instead — matches the
        # "unparseable input" path above.
        if not math.isfinite(raw):
            raw = 0.5
        calibrated = apply_calibration(raw, tmpl.calibration_curve)
        if not math.isfinite(calibrated):
            # Defensive: shouldn't happen given apply_calibration
            # clamps to [0, 1], but if a corrupt curve somehow
            # produced NaN, drop the calibrated field rather than
            # write a misleading number.
            continue
        rec["calibrated_confidence"] = round(calibrated, 4)


async def _attach_baseline_delta(
    db: AsyncSession,
    discussion: Discussion,
    conclusion: dict[str, Any],
) -> None:
    """PR-5c: annotate the conclusion with a ``vs_baseline`` block
    that lets the UI render "this conclusion's confidence is N%
    higher/lower than the strategy's 30-day baseline" without a
    second round-trip.

    Mutates ``conclusion`` in place. Skipped when:
      - the discussion has no sweep parent (live mode)
      - the parent sweep has no strategy_id
      - the strategy has no health snapshot yet (cold start)
      - the conclusion is a parse-error placeholder

    ``vs_baseline`` shape::

        {
          "brier_baseline": 0.18,        # latest snapshot's brier_30d
          "consensus_baseline": 0.62,    # avg consensus_score over the last
                                          # N concluded discussions
          "consensus_score": 0.75,       # this conclusion's value
          "consensus_pct_change": 21.0,  # (this - baseline) / baseline * 100
          "verify_pending": true,        # brier comparison is filled by the
                                          # verifier 5d after as_of; until
                                          # then the badge says "待 5 日後驗證"
        }

    Best-effort throughout: any failure logs but doesn't reverse
    the conclusion write — it's a UI hint, not a contract.
    """
    if conclusion.get("_parse_error"):
        return
    sweep_id = getattr(discussion, "sweep_id", None)
    if sweep_id is None:
        return
    try:
        from models.backtest_sweep import BacktestSweep
        from models.discussion_strategy_template import (
            DiscussionStrategyTemplate,
        )
        from services import strategy_health_service
        sweep_row = await db.scalar(
            select(BacktestSweep).where(BacktestSweep.id == sweep_id)
        )
        if sweep_row is None or sweep_row.strategy_id is None:
            return
        tmpl = await db.scalar(
            select(DiscussionStrategyTemplate).where(
                DiscussionStrategyTemplate.id == sweep_row.strategy_id,
            )
        )
        if tmpl is None:
            return

        recent_snapshots = await strategy_health_service.list_recent_snapshots(
            db, strategy_id=sweep_row.strategy_id, days=30,
        )
        baseline_brier: float | None = None
        for snap in recent_snapshots:
            if snap.brier_30d is not None:
                baseline_brier = float(snap.brier_30d)
                break

        # Avg consensus over the last 20 concluded discussions of
        # this strategy (excluding the current one). Cheap aggregate.
        from models.discussion import Discussion as _Discussion
        sweep_ids = list((await db.scalars(
            select(BacktestSweep.id).where(
                BacktestSweep.strategy_id == sweep_row.strategy_id,
            )
        )).all())
        consensus_baseline: float | None = None
        if sweep_ids:
            recent_concs = list((await db.scalars(
                select(_Discussion.conclusion)
                .where(
                    _Discussion.sweep_id.in_(sweep_ids),
                    _Discussion.id != discussion.id,
                    _Discussion.conclusion.is_not(None),
                )
                .order_by(_Discussion.created_at.desc())
                .limit(20)
            )).all())
            scores: list[float] = []
            for c in recent_concs:
                if isinstance(c, dict):
                    try:
                        v = float(c.get("consensus_score", 0.0))
                        if 0.0 <= v <= 1.0:
                            scores.append(v)
                    except (TypeError, ValueError):
                        continue
            if scores:
                consensus_baseline = sum(scores) / len(scores)

        try:
            this_consensus = float(conclusion.get("consensus_score", 0.0))
        except (TypeError, ValueError):
            this_consensus = 0.0
        consensus_pct_change: float | None = None
        if consensus_baseline is not None and consensus_baseline > 0:
            consensus_pct_change = (
                (this_consensus - consensus_baseline)
                / consensus_baseline * 100.0
            )

        conclusion["vs_baseline"] = {
            "brier_baseline": (
                round(baseline_brier, 4) if baseline_brier is not None else None
            ),
            "consensus_baseline": (
                round(consensus_baseline, 4)
                if consensus_baseline is not None else None
            ),
            "consensus_score": round(this_consensus, 4),
            "consensus_pct_change": (
                round(consensus_pct_change, 2)
                if consensus_pct_change is not None else None
            ),
            # Brier comparison waits for the verifier (5d post as_of).
            # The frontend reads this flag to show "待驗證" instead
            # of an empty cell.
            "verify_pending": True,
        }
    except Exception as exc:
        log.warning(
            "discussion.baseline_delta.failed",
            extra={
                "discussion_id": str(discussion.id),
                "error": str(exc),
            },
        )


async def _compute_quality_signals(
    db: AsyncSession,
    discussion: Discussion,
    conclusion: dict[str, Any],
    turns: list[DiscussionTurn],
) -> None:
    """PR-1: attach a ``quality_signals`` block to the conclusion JSON.

    The synthesizer prompt asks the LLM not to give every pick
    confidence ≥ 0.8, but ``_safe_conclusion`` only clamps to [0,1]
    without checking the distribution. This helper surfaces four
    signals so the UI can warn the operator about outputs that
    look structurally ok but smell wrong:

      - ``stance_distribution``: agree / dissent / supplement count
        in the latest round (excludes user_input directives).
        Lets the UI flag the case where a "buy" recommendation was
        synthesized over a majority-dissent transcript.
      - ``confidence_stats``: n / mean / median / max / min over
        recommendations, with ``over_confident=True`` when mean > 0.75
        OR every pick ≥ 0.8 — promotes the prompt-level guidance to
        a post-parse signal.
      - ``consensus_contradiction``: bool, dissent > agree AND
        recommendations non-empty.
      - ``observed_consensus`` / ``consensus_gap``: agreement measured
        from the transcript (all rounds, later rounds weighted higher)
        and how far the synthesizer's self-reported
        ``consensus_score`` sits above it. See
        ``services/discussion/consensus.py`` — nothing previously
        checked the self-reported number against what was said, and in
        production the two are decoupled.
      - ``hallucination_warnings``: from
        ``signal_audit_service.audit_discussion_for_synthesis`` —
        triples of (round, persona_id, signal) where a persona
        cited a numeric value for a signal NOT in the round's
        prompt context. Best-effort: audit failure logs but doesn't
        block synthesis.

    Mutates ``conclusion`` in place. Skipped (writes a ``_skipped``
    marker) on parse-error placeholders so the UI can distinguish
    "couldn't parse the LLM output" from "parsed but no warnings".
    """
    if conclusion.get("_parse_error"):
        conclusion["quality_signals"] = {"_skipped": "parse_error"}
        return

    # Lazy-import the user-persona sentinel to keep the synthesizer
    # module load-time-acyclic — `discussion_service` re-exports
    # `_compute_quality_signals` and we don't want a module-import
    # ouroboros at startup.
    from services.discussion_service import USER_PERSONA_ID

    persona_turns = [t for t in turns if t.persona_id != USER_PERSONA_ID]
    latest_round = max((t.round for t in persona_turns), default=0)
    latest_turns = [t for t in persona_turns if t.round == latest_round]

    stance_dist = {"agree": 0, "dissent": 0, "supplement": 0, "other": 0}
    for t in latest_turns:
        bucket = t.stance if t.stance in stance_dist else "other"
        stance_dist[bucket] += 1

    confidences: list[float] = []
    for rec in conclusion.get("recommendations") or []:
        if not isinstance(rec, dict):
            continue
        try:
            v = float(rec.get("confidence", 0.5))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        confidences.append(max(0.0, min(1.0, v)))

    confidence_stats: dict[str, Any] = {"n": len(confidences)}
    if confidences:
        n = len(confidences)
        sorted_c = sorted(confidences)
        mean_c = sum(sorted_c) / n
        median_c = (
            sorted_c[n // 2]
            if n % 2 == 1
            else (sorted_c[n // 2 - 1] + sorted_c[n // 2]) / 2
        )
        confidence_stats.update({
            "mean": round(mean_c, 4),
            "median": round(median_c, 4),
            "max": round(sorted_c[-1], 4),
            "min": round(sorted_c[0], 4),
            "over_confident": (
                mean_c > 0.75 or all(c >= 0.8 for c in sorted_c)
            ),
        })

    consensus_contradiction = (
        stance_dist["dissent"] > stance_dist["agree"]
        and bool(conclusion.get("recommendations"))
    )

    # Measured agreement across ALL rounds, vs the number the
    # synthesizer wrote about itself. `stance_distribution` above only
    # covers the final round, which is the right input for the
    # contradiction flag but too narrow to compare against a
    # whole-discussion score.
    from services.discussion.consensus import consensus_gap, observed_consensus

    observed = observed_consensus(persona_turns)
    gap = consensus_gap(conclusion.get("consensus_score"), observed)

    warnings: list[dict[str, Any]] = []
    try:
        from services.signal_audit_service import (
            audit_discussion_for_synthesis,
        )
        warnings = await audit_discussion_for_synthesis(db, discussion.id)
    except Exception as exc:
        log.warning(
            "discussion.quality_signals.audit_failed",
            extra={
                "discussion_id": str(discussion.id),
                "error": str(exc),
            },
        )

    conclusion["quality_signals"] = {
        "stance_distribution": stance_dist,
        "confidence_stats": confidence_stats,
        "consensus_contradiction": consensus_contradiction,
        "observed_consensus": observed,
        "consensus_gap": gap,
        "hallucination_warnings": warnings,
    }


async def synthesize_conclusion(
    db: AsyncSession,
    discussion: Discussion,
    *,
    user_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Read every turn and produce a structured conclusion JSON. Stores
    the result on the Discussion row and flips status → done.

    The synthesizer is intentionally a fixed task (not one of the persona
    LLMs) — we want a neutral arbiter, not a re-skin of Buffett or Soros.
    Provider/model default to whatever the admin selected for the
    ``discussion_synthesizer`` system task; pass explicit values in tests
    or one-off calls to bypass the resolver.
    """
    # CRUD entry-points + status/user-persona sentinels stay in
    # `discussion_service` proper; lazy-import keeps the load graph
    # acyclic per the module docstring.
    #
    # `stream_chat` is re-imported from `discussion_service`'s namespace
    # (not directly from `ai.llm_router`) so test fixtures that
    # `patch("services.discussion_service.stream_chat", ...)` to mock
    # the LLM also intercept the synthesizer's call. ~50 test sites
    # use that path; updating each to also patch the synthesizer
    # module would be churn for no functional gain.
    from services.discussion_service import (
        STATUS_DONE,
        USER_PERSONA_ID,
        gather_market_context,
        get_round_contexts,
        get_turns,
        stream_chat,
    )

    if provider is None or model is None:
        from services.system_task_config_service import resolve as _resolve_task
        r_provider, r_model = await _resolve_task(db, "discussion_synthesizer")
        provider = provider or r_provider
        model = model or r_model
    turns = await get_turns(db, discussion_id=discussion.id)
    # Reuse the most recent round's context snapshot so the synthesiser
    # reasons over the same evidence the personas saw, instead of
    # pulling a fresh `gather_market_context` (which would silently
    # drift if the round was run pre-close and the synthesise runs
    # post-close, or if a connector started failing in between).
    # Falls back to a fresh fetch only for legacy discussions whose
    # rounds predate the round-context snapshot table — keeps old
    # rows synthesisable without manual backfill.
    snapshots = await get_round_contexts(db, discussion_id=discussion.id)
    if snapshots:
        context = snapshots[-1].context
    else:
        focus = extract_focus_symbols(
            discussion.topic, market=discussion.market,
        )
        context = await gather_market_context(
            db,
            market=discussion.market,
            focus_symbols=focus,
            owner_id=discussion.owner_id,
            exclude_discussion_id=discussion.id,
            as_of=discussion.as_of_date,
            topic=discussion.topic,
        )

    # PR #267: detect a post-mortem self-critique cycle in the
    # transcript so the synthesizer prompt can call it out and the
    # token budget can be sized appropriately. Without this guidance
    # the synthesizer treats the round-2 reflections as just more
    # opinions and silently double-weights the original round-1
    # consensus; with longer post-mortem transcripts the JSON also
    # tends to truncate within the default budget.
    has_post_mortem = any(
        t.persona_id == USER_PERSONA_ID
        and (t.content or "").lstrip().startswith("【事後檢討")
        for t in turns
    )

    user_prompt = _SYNTHESIZER_USER_TEMPLATE.format(
        topic=discussion.topic,
        rules=discussion.rules,
        freshness_preamble=_format_freshness_preamble(context),
        # Compact separators (no indent) to cut prompt input tokens;
        # JSON semantics are unchanged.
        context=json.dumps(
            _minify_for_prompt(context), ensure_ascii=False, separators=(",", ":"),
        ),
        transcript=_format_transcript(turns),
    )

    # PR-C: when this discussion was spawned by a sweep whose
    # parent strategy has learned persona weights, surface them as
    # a tie-breaker hint so the synthesizer can lean on personas
    # with a track record. Returns "" when no weights are available
    # (live discussions, sweeps not tied to a template, fresh
    # templates not yet trained).
    #
    # PR-A1: when the sweep carries `weights_override` (set by the
    # walk-forward orchestrator on a test fold), it takes priority
    # over the parent template's `persona_weights`. This is what
    # actually executes the OOS evaluation: the test fold runs
    # against weights frozen from its train sibling instead of
    # the live (and potentially in-sample-trained) template.
    if discussion.sweep_id is not None:
        from models.backtest_sweep import BacktestSweep
        from models.discussion_strategy_template import (
            DiscussionStrategyTemplate,
        )
        from services.persona_weight_learner import (
            format_weights_for_synthesizer,
        )
        sweep_row = await db.scalar(
            select(BacktestSweep).where(BacktestSweep.id == discussion.sweep_id)
        )
        weights_to_use: dict[str, float] | None = None
        if sweep_row is not None:
            override = getattr(sweep_row, "weights_override", None)
            if override:
                weights_to_use = dict(override)
            elif sweep_row.strategy_id is not None:
                tmpl = await db.scalar(
                    select(DiscussionStrategyTemplate).where(
                        DiscussionStrategyTemplate.id == sweep_row.strategy_id,
                    )
                )
                if tmpl is not None and tmpl.persona_weights:
                    weights_to_use = dict(tmpl.persona_weights)
        if weights_to_use:
            user_prompt += format_weights_for_synthesizer(weights_to_use)

    if has_post_mortem:
        user_prompt += (
            "\n\n## 補充提示（事後檢討模式）\n"
            "本場討論已包含一輪「事後檢討」（基於實際漲幅排行榜的反思），"
            "請以 personas 在事後檢討輪 (latest round) 的最終立場為主，"
            "整理出修正後的結論。\n"
            "**仍然只能輸出合法 JSON**，不要回答事後檢討提示中的問題、"
            "不要在 JSON 外加任何解說。\n"
            "\n"
            "**額外輸出 `lessons` 欄位**：在 conclusion JSON 內加入 "
            '`"lessons": [...]` 陣列（最多 5 條），把這場事後檢討'
            "學到的事拆成可重用的教訓供未來討論參考。每條 shape：\n"
            "  - `category`：missed_sector | wrong_signal_weight | "
            "missing_data | over_confidence | other 五選一\n"
            "  - `lesson_text`：**繁體中文 ≤ 80 字**，具體點名訊號 / 產業 / "
            "股票，避免「下次要更小心」這類空話\n"
            "  - `related_symbols`：教訓涉及的股票代號（陣列，可空）\n"
            "  - `missed_winners`：漲幅榜上錯過的代號（陣列，可空）\n"
            "若這場討論沒有具體可重用的教訓（純粹是 random walk），"
            "回傳空陣列 `\"lessons\": []`，不要硬擠。"
        )

    # R6 PR2 devil's-advocate critic (off by default): before committing
    # to a conclusion, surface the strongest case AGAINST the emerging
    # consensus and require the conclusion to engage it. Best-effort — any
    # failure leaves `user_prompt` untouched so synthesis is unchanged.
    if settings.DISCUSSION_SYNTH_CRITIC_ENABLED:
        try:
            from services.discussion.critic import generate_devils_advocate
            # Bound the aux-model call so a hung provider can't stall the
            # conclusion; timeout is caught below → critique None → unchanged.
            async with asyncio.timeout(settings.DISCUSSION_AUX_MODEL_TIMEOUT_S):
                critique = await generate_devils_advocate(
                    db,
                    topic=discussion.topic,
                    transcript=_format_transcript(turns),
                    user_id=user_id,
                    discussion_id=discussion.id,
                )
        except Exception:
            critique = None
            log.warning(
                "discussion.critic.pipeline_failed",
                extra={"id": str(discussion.id)},
            )
        if critique:
            user_prompt += (
                "\n\n## 反方觀點(魔鬼代言人 · 必須回應)\n"
                "以下是針對本場浮現共識的最強反面論證。你的 `reasoning` 必須"
                "明確回應這些反對點(採納並修正結論,或逐點反駁並說明為何不"
                "影響結論),不得忽略或跳過:\n"
                f"{critique}"
            )

    messages = [
        {"role": "system", "content": _SYNTHESIZER_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    assembled = ""
    usage_seen: dict[str, int] | None = None
    # `max_tokens` is admin-tunable via RuntimeTunablesCard
    # (`DISCUSSION_SYNTHESIZER_MAX_TOKENS`, default 8192). Default gives
    # reasoning models enough room for chain-of-thought (~3-5K tokens)
    # before the conclusion JSON (~1.5K tokens) is emitted.
    try:
        from services.runtime_config_service import get_int as _runtime_get_int
        synth_max_tokens = await _runtime_get_int(
            db, "DISCUSSION_SYNTHESIZER_MAX_TOKENS",
        )
    except Exception as exc:
        log.warning("discussion.runtime_config_failed",
                    extra={"setting": "DISCUSSION_SYNTHESIZER_MAX_TOKENS",
                           "error": str(exc)})
        synth_max_tokens = settings.DISCUSSION_SYNTHESIZER_MAX_TOKENS

    # Post-mortem transcripts are 1.5-2x longer than a single-round
    # discussion (round-1 turns + post-mortem prompt + round-2
    # self-critique replies). Reasoning models need proportionally
    # more output room or the JSON gets truncated mid-object and
    # `_safe_conclusion` falls back to the parse-error placeholder.
    # Bump 50% as a conservative safety margin.
    if has_post_mortem:
        synth_max_tokens = int(synth_max_tokens * 1.5)
    try:
        async for event in stream_chat(
            messages=messages,
            provider=provider,
            model=model,
            max_tokens=synth_max_tokens,
            temperature=0.2,
            db=db,
            user_id=user_id,
        ):
            etype = event.get("type")
            if etype == "delta":
                assembled += event.get("text", "")
            elif etype == "usage":
                # Accumulate (PR #216) — synth doesn't usually run a
                # tool loop today (system task uses a vanilla provider),
                # but the same accumulator pattern protects us when an
                # admin retargets the synthesizer at a tool-capable
                # provider via SystemTasksCard.
                if usage_seen is None:
                    usage_seen = {"prompt_tokens": 0, "completion_tokens": 0}
                usage_seen["prompt_tokens"] += int(
                    event.get("prompt_tokens", 0)
                )
                usage_seen["completion_tokens"] += int(
                    event.get("completion_tokens", 0)
                )
            elif etype == "error":
                log.warning(
                    "discussion.synthesize.llm_error",
                    # `message` is a reserved LogRecord attribute — putting
                    # it in `extra` raises KeyError inside logging.
                    extra={"detail": event.get("message")},
                )
                break
    except Exception as exc:
        log.exception("discussion.synthesize.failed", extra={"id": str(discussion.id)})
        assembled = json.dumps({"reasoning": f"合成失敗：{exc}"})

    if usage_seen is not None:
        from services.llm_usage_service import record_usage
        await record_usage(
            db,
            user_id=user_id,
            provider=provider,
            model=model,
            persona_id="_system:discussion_synthesizer",
            prompt_tokens=usage_seen["prompt_tokens"],
            completion_tokens=usage_seen["completion_tokens"],
            # Attribute to the discussion but not to any single round
            # (synthesis runs after the rounds) so per-round tallies stay clean.
            discussion_id=discussion.id,
            round=None,
        )

    conclusion = _safe_conclusion(assembled)
    # Carry the ctx's data-freshness anchor through to the conclusion so
    # the frontend ConclusionCard can render a "資料截至 X 收盤" badge
    # without re-fetching the round-context snapshot. Mirrors the same
    # `captured_session` block the personas saw during synthesis — the
    # discussion's authoritative answer to "what session does this
    # conclusion describe?"
    sess = (context or {}).get("captured_session")
    if isinstance(sess, dict):
        conclusion["captured_session"] = sess
    # PR-1: attach stance / confidence / contradiction / hallucination
    # quality signals to the conclusion JSON so the UI can warn the
    # operator about outputs that look structurally ok (parses, has
    # picks) but smell wrong (built on hallucinated data, contradicts
    # the actual stance distribution, all picks ≥ 0.8). Computed
    # before calibration so downstream consumers see signals on the
    # raw confidence the LLM emitted.
    await _compute_quality_signals(db, discussion, conclusion, turns)
    # PR-C2 follow-up: if this discussion is part of a strategy that
    # has accumulated enough samples for calibration, map every
    # raw confidence through the isotonic curve and store both
    # values. The synthesizer's emitted confidence (`confidence`)
    # is preserved for telemetry / future re-fits; downstream
    # consumers (UI, scoreboard Brier, calibration audit) read
    # `calibrated_confidence` for the post-correction value. Only
    # touches when a curve exists — bare strategies + cold-start
    # behave identically to today.
    await _apply_calibration_to_conclusion(db, discussion, conclusion)
    # PR-5c: when the discussion is sweep-spawned, attach a
    # `vs_baseline` block summarising how this conclusion compares
    # to the strategy's recent rolling baseline. Best-effort —
    # missing health snapshot / no sweep parent / no strategy =
    # silently skip (the field stays absent, frontend doesn't
    # render the badge).
    await _attach_baseline_delta(db, discussion, conclusion)
    # PR #272: route the write based on whether the transcript already
    # carries a post-mortem self-critique. With post-mortem present,
    # land the synthesizer's output in `post_mortem_conclusion` so
    # the original `conclusion` is preserved for side-by-side
    # comparison in the UI. Without post-mortem, the existing
    # behaviour (overwrite `conclusion`) is unchanged.
    if has_post_mortem:
        discussion.post_mortem_conclusion = conclusion
        # PR-2: pre-compute the structured diff between original and
        # revised conclusion so the UI can render "what changed"
        # without recomputing on every read. None when either side
        # is unparseable — we'd rather leave the column NULL than
        # write a misleading partial diff.
        diff = compute_conclusion_diff(discussion.conclusion, conclusion)
        if diff is not None:
            discussion.post_mortem_diff = diff
    else:
        discussion.conclusion = conclusion
    discussion.status = STATUS_DONE
    discussion.updated_at = datetime.now(UTC)
    # Seed `verify_after_date` so the verifier task picks this row up
    # in 5 trading days (PR #218). Used to be set only by the auto-run
    # cron, which left manual discussions permanently un-graded —
    # `prior_discussions` then surfaced `verdict=null` for nearly
    # every cross-session reference, defeating the consistency check.
    # Skip when already set (re-conclude after edit) so we don't
    # push the verification window back artificially.
    #
    # The verifier still grades the ORIGINAL conclusion's recommended
    # symbols — post_mortem_conclusion is informational. If we ever
    # want the verifier to use the post-mortem version instead, we'd
    # update score_discussion_outcomes to prefer the latter when
    # populated; that's intentionally NOT in this PR.
    if discussion.verify_after_date is None:
        from services.tw_trading_calendar import (
            add_trading_days_estimate,
            utcnow_tw_date,
        )
        # 5 trading days matches the verifier's `_WINDOW_TRADING_DAYS`
        # — anything sooner and the bars haven't all resolved yet.
        # In backtest mode (PR #224), anchor on `as_of_date` instead
        # of today so the post-window is the historical 5 trading
        # days after the backtest anchor — verifier picks the row
        # up immediately if as_of + 5d is already in the past.
        anchor = discussion.as_of_date or utcnow_tw_date()
        discussion.verify_after_date = add_trading_days_estimate(anchor, 5)
    await db.commit()
    await db.refresh(discussion)

    # Best-effort: extract structured lessons from the post-mortem
    # synthesizer pass + persist them for the learning loop. Only
    # fires under post-mortem mode (the prompt's `lessons` ask is
    # only included then) and only for backtest discussions
    # (`as_of_date` carries the anchor needed for time-decay
    # scoring). Failure here is non-fatal — the conclusion is
    # already committed; missing a write just means this run
    # didn't contribute to the learning archive.
    if has_post_mortem and discussion.as_of_date is not None:
        try:
            raw_obj = _extract_lessons_payload(assembled)
            if raw_obj:
                from services.discussion_lesson_service import (
                    extract_and_persist_lessons,
                )
                await extract_and_persist_lessons(
                    db,
                    discussion_id=discussion.id,
                    owner_user_id=discussion.owner_id,
                    market=discussion.market,
                    as_of_date=discussion.as_of_date,
                    lessons_payload=raw_obj,
                    ctx=context,
                    verdict=discussion.verdict,
                )
        except Exception as exc:
            log.warning("discussion.lessons.persist_failed",
                        extra={"id": str(discussion.id),
                               "error": str(exc)})
    return conclusion
