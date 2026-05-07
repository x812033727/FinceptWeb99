"""PR-1: tests for `_compute_quality_signals` — the synthesizer
post-parse hook that surfaces stance / confidence / contradiction /
hallucination warnings on the conclusion JSON.

These are unit tests for the helper itself. The integration with
`synthesize_conclusion` is exercised by tests in
`test_discussion_service.py` that already mock the LLM stream — the
helper is designed to be safe to call standalone with a turns list
+ conclusion dict, so we test the logic at that level.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion, DiscussionTurn
from models.discussion_round_context import DiscussionRoundContext
from models.user import User, UserRole
from services import discussion_service


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"qsig-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _make_discussion(owner_id: uuid.UUID) -> Discussion:
    return Discussion(
        id=uuid4(), owner_id=owner_id, topic="t", rules="",
        market="TW", persona_ids=["a", "b"],
        status="concluded", current_round=1,
    )


def _turn(round_no: int, persona_id: str, stance: str, content: str = "") -> DiscussionTurn:
    return DiscussionTurn(
        discussion_id=uuid4(),
        round=round_no,
        turn_index=0,
        persona_id=persona_id,
        stance=stance,
        content=content,
    )


# ── parse-error short-circuit ─────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_when_parse_error(db_session: AsyncSession, owner: User):
    """A parse-error placeholder shouldn't fan out to audit — write
    a `_skipped` marker instead so the UI can distinguish 'parsing
    broke' from 'parsed cleanly with no warnings'."""
    disc = _make_discussion(owner.id)
    conclusion: dict[str, Any] = {
        "_parse_error": True,
        "recommendations": [],
    }
    await discussion_service._compute_quality_signals(
        db_session, disc, conclusion, turns=[],
    )
    assert conclusion["quality_signals"] == {"_skipped": "parse_error"}


# ── stance distribution ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_stance_distribution_counts_latest_round_only(
    db_session: AsyncSession, owner: User,
):
    """`current_round` is round 2; round-1 stances should NOT count.
    User-input directives (USER_PERSONA_ID) are excluded so a
    post-mortem prompt doesn't skew the bucket."""
    disc = _make_discussion(owner.id)
    turns = [
        _turn(1, "a", "agree"),
        _turn(1, "b", "agree"),
        _turn(2, "a", "dissent"),
        _turn(2, "b", "dissent"),
        _turn(2, "c", "supplement"),
        _turn(2, discussion_service.USER_PERSONA_ID, "agree", "【事後檢討..."),
    ]
    conclusion: dict[str, Any] = {"recommendations": []}
    with patch(
        "services.signal_audit_service.audit_discussion_for_synthesis",
        AsyncMock(return_value=[]),
    ):
        await discussion_service._compute_quality_signals(
            db_session, disc, conclusion, turns=turns,
        )
    qs = conclusion["quality_signals"]
    # latest round = 2; user_input excluded
    assert qs["stance_distribution"] == {
        "agree": 0, "dissent": 2, "supplement": 1, "other": 0,
    }


# ── confidence stats ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_over_confident_when_mean_above_threshold(
    db_session: AsyncSession, owner: User,
):
    """All five picks ≥ 0.8 → over_confident True even though mean
    might fall right at 0.8."""
    disc = _make_discussion(owner.id)
    conclusion: dict[str, Any] = {
        "recommendations": [
            {"symbol": "2330", "confidence": 0.85},
            {"symbol": "2454", "confidence": 0.82},
            {"symbol": "2317", "confidence": 0.80},
            {"symbol": "0050", "confidence": 0.81},
            {"symbol": "2412", "confidence": 0.83},
        ],
    }
    with patch(
        "services.signal_audit_service.audit_discussion_for_synthesis",
        AsyncMock(return_value=[]),
    ):
        await discussion_service._compute_quality_signals(
            db_session, disc, conclusion, turns=[],
        )
    cs = conclusion["quality_signals"]["confidence_stats"]
    assert cs["n"] == 5
    assert cs["over_confident"] is True
    assert cs["max"] == 0.85
    assert cs["min"] == 0.80


@pytest.mark.asyncio
async def test_balanced_confidence_not_flagged(
    db_session: AsyncSession, owner: User,
):
    """Mean 0.5 + spread → over_confident should be False."""
    disc = _make_discussion(owner.id)
    conclusion: dict[str, Any] = {
        "recommendations": [
            {"symbol": "2330", "confidence": 0.7},
            {"symbol": "2454", "confidence": 0.4},
            {"symbol": "2317", "confidence": 0.5},
        ],
    }
    with patch(
        "services.signal_audit_service.audit_discussion_for_synthesis",
        AsyncMock(return_value=[]),
    ):
        await discussion_service._compute_quality_signals(
            db_session, disc, conclusion, turns=[],
        )
    cs = conclusion["quality_signals"]["confidence_stats"]
    assert cs["over_confident"] is False
    assert cs["mean"] == pytest.approx(0.5333, abs=1e-3)


@pytest.mark.asyncio
async def test_no_recommendations_yields_empty_confidence_stats(
    db_session: AsyncSession, owner: User,
):
    disc = _make_discussion(owner.id)
    conclusion: dict[str, Any] = {"recommendations": []}
    with patch(
        "services.signal_audit_service.audit_discussion_for_synthesis",
        AsyncMock(return_value=[]),
    ):
        await discussion_service._compute_quality_signals(
            db_session, disc, conclusion, turns=[],
        )
    cs = conclusion["quality_signals"]["confidence_stats"]
    assert cs == {"n": 0}


# ── consensus contradiction ──────────────────────────────────────


@pytest.mark.asyncio
async def test_consensus_contradiction_when_dissent_majority_with_picks(
    db_session: AsyncSession, owner: User,
):
    """6 dissent + 2 agree but synthesizer still emitted picks =
    structural contradiction worth surfacing to the operator."""
    disc = _make_discussion(owner.id)
    turns = [_turn(1, f"p{i}", "dissent") for i in range(6)] + [
        _turn(1, "p6", "agree"), _turn(1, "p7", "agree"),
    ]
    conclusion: dict[str, Any] = {
        "recommendations": [{"symbol": "2330", "confidence": 0.65}],
    }
    with patch(
        "services.signal_audit_service.audit_discussion_for_synthesis",
        AsyncMock(return_value=[]),
    ):
        await discussion_service._compute_quality_signals(
            db_session, disc, conclusion, turns=turns,
        )
    qs = conclusion["quality_signals"]
    assert qs["consensus_contradiction"] is True


@pytest.mark.asyncio
async def test_no_contradiction_when_dissent_majority_but_no_picks(
    db_session: AsyncSession, owner: User,
):
    """6 dissent + 2 agree but recommendations=[] is consistent —
    don't flag (the synthesizer correctly refused to recommend)."""
    disc = _make_discussion(owner.id)
    turns = [_turn(1, f"p{i}", "dissent") for i in range(6)] + [
        _turn(1, "p6", "agree"), _turn(1, "p7", "agree"),
    ]
    conclusion: dict[str, Any] = {"recommendations": []}
    with patch(
        "services.signal_audit_service.audit_discussion_for_synthesis",
        AsyncMock(return_value=[]),
    ):
        await discussion_service._compute_quality_signals(
            db_session, disc, conclusion, turns=turns,
        )
    qs = conclusion["quality_signals"]
    assert qs["consensus_contradiction"] is False


# ── hallucination wiring ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_hallucination_warnings_pulled_through(
    db_session: AsyncSession, owner: User,
):
    """When the audit service flags a hallucination, surface the
    triples in `hallucination_warnings` verbatim."""
    disc = _make_discussion(owner.id)
    fake_warnings = [
        {"round": 1, "persona_id": "soros", "signal": "single_stock_futures_oi"},
    ]
    with patch(
        "services.signal_audit_service.audit_discussion_for_synthesis",
        AsyncMock(return_value=fake_warnings),
    ):
        conclusion: dict[str, Any] = {"recommendations": []}
        await discussion_service._compute_quality_signals(
            db_session, disc, conclusion, turns=[],
        )
    assert conclusion["quality_signals"]["hallucination_warnings"] == fake_warnings


@pytest.mark.asyncio
async def test_audit_failure_logged_not_propagated(
    db_session: AsyncSession, owner: User,
):
    """Audit raises → empty warnings, conclusion still gets
    quality_signals (best-effort contract)."""
    disc = _make_discussion(owner.id)
    with patch(
        "services.signal_audit_service.audit_discussion_for_synthesis",
        AsyncMock(side_effect=RuntimeError("DB exploded")),
    ):
        conclusion: dict[str, Any] = {"recommendations": []}
        await discussion_service._compute_quality_signals(
            db_session, disc, conclusion, turns=[],
        )
    qs = conclusion["quality_signals"]
    assert qs["hallucination_warnings"] == []
    # other signals still computed
    assert "stance_distribution" in qs
    assert "confidence_stats" in qs


# ── end-to-end through audit_discussion_for_synthesis ────────────


@pytest.mark.asyncio
async def test_real_audit_path_writes_hallucination_for_seeded_turn(
    db_session: AsyncSession, owner: User,
):
    """Don't mock the audit — seed a real DiscussionRoundContext +
    a turn that quotes a numeric value for an absent signal, and
    confirm `_compute_quality_signals` propagates it through the
    real `audit_discussion_for_synthesis` path."""
    disc_id = uuid4()
    disc = Discussion(
        id=disc_id, owner_id=owner.id, topic="2330", rules="",
        market="TW", persona_ids=["market_analyst", "soros"],
        status="concluded", current_round=1,
    )
    db_session.add(disc)
    db_session.add(DiscussionRoundContext(
        discussion_id=disc_id, round=1,
        context={"taifex_positioning": {"trend": "neutral"}},
        captured_at=datetime.now(UTC),
    ))
    turns = [
        DiscussionTurn(
            discussion_id=disc_id, round=1, turn_index=0,
            persona_id="market_analyst", stance="agree", content="持平。",
        ),
        DiscussionTurn(
            discussion_id=disc_id, round=1, turn_index=1,
            persona_id="soros", stance="dissent",
            content="個股期 2330 多單 +1500 口。",
        ),
    ]
    db_session.add_all(turns)
    await db_session.commit()

    conclusion: dict[str, Any] = {
        "recommendations": [{"symbol": "2330", "confidence": 0.6}],
    }
    await discussion_service._compute_quality_signals(
        db_session, disc, conclusion, turns=turns,
    )
    warns = conclusion["quality_signals"]["hallucination_warnings"]
    assert any(
        w["persona_id"] == "soros"
        and w["signal"] == "single_stock_futures_oi"
        for w in warns
    )
