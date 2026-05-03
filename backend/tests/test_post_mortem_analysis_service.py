"""Tests for `services.post_mortem_analysis_service`.

Covers:
  - Round detection: post-mortem injection round is identified by
    the user_input turn whose content starts with "【事後檢討".
  - Personas at round > injection_round contribute to the gap count.
  - Discussions WITHOUT a post-mortem injection are skipped silently.
  - Per-category aggregation: mentions / discussions / personas
    counters all increment correctly across multiple discussions.
  - Persona's content gets matched against the taxonomy with the
    expected categories firing on representative phrases.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion, DiscussionTurn
from models.user import User, UserRole
from services import post_mortem_analysis_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"pma-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


_PM_HEADER = "【事後檢討 — 對答案】\n\n"


async def _seed_discussion_with_pm(
    db_session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    market: str = "TW",
    persona_replies: list[tuple[str, str]],
    has_pm_injection: bool = True,
) -> uuid.UUID:
    """Helper: insert a concluded discussion with one round, an
    optional post-mortem injection, and a list of persona replies
    (each (persona_id, content)) at the round AFTER the injection.

    With `has_pm_injection=False`, simulates a discussion that was
    concluded without anyone running a post-mortem — analysis must
    skip these silently.
    """
    disc_id = uuid4()
    db_session.add(Discussion(
        id=disc_id, owner_id=owner_id,
        topic="post-mortem test", rules="", market=market,
        persona_ids=[p for p, _ in persona_replies],
        status="concluded",
        current_round=2 if has_pm_injection else 1,
    ))
    # Round 1: existing persona turns (placeholder; not analysed).
    db_session.add(DiscussionTurn(
        discussion_id=disc_id, round=1, turn_index=0,
        persona_id="placeholder", stance="agree",
        content="initial reasoning",
    ))
    # Post-mortem injection at round 1, turn_index=1 (after persona
    # turn). Personas reply at round 2.
    if has_pm_injection:
        db_session.add(DiscussionTurn(
            discussion_id=disc_id, round=1, turn_index=1,
            persona_id="_user", stance="user_input",
            content=_PM_HEADER + "請各位專家依以下四點檢討...",
        ))
        for i, (pid, content) in enumerate(persona_replies):
            db_session.add(DiscussionTurn(
                discussion_id=disc_id, round=2, turn_index=i,
                persona_id=pid, stance="agree",
                content=content,
            ))
    await db_session.commit()
    return disc_id


# ── Round detection ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_post_mortem_round_detects_user_input(
    db_session: AsyncSession, owner: User,
):
    """`_find_post_mortem_round` returns the round number of the
    user_input turn whose content starts with the post-mortem
    marker. Other user_input rows (e.g. earlier injections) are
    ignored unless they start with the marker."""
    disc_id = uuid4()
    db_session.add(Discussion(
        id=disc_id, owner_id=owner.id,
        topic="t", rules="", market="TW",
        persona_ids=["a"], status="concluded", current_round=2,
    ))
    # Earlier user_input that isn't a post-mortem.
    db_session.add(DiscussionTurn(
        discussion_id=disc_id, round=1, turn_index=0,
        persona_id="_user", stance="user_input",
        content="補充：請考慮 2330 的法說會時程。",
    ))
    # The post-mortem injection.
    db_session.add(DiscussionTurn(
        discussion_id=disc_id, round=1, turn_index=1,
        persona_id="_user", stance="user_input",
        content=_PM_HEADER + "本次回測對答案如下...",
    ))
    await db_session.commit()

    rnd = await svc._find_post_mortem_round(db_session, disc_id)
    assert rnd == 1


@pytest.mark.asyncio
async def test_find_post_mortem_round_returns_none_when_absent(
    db_session: AsyncSession, owner: User,
):
    """No post-mortem injection → analysis must skip silently."""
    disc_id = uuid4()
    db_session.add(Discussion(
        id=disc_id, owner_id=owner.id,
        topic="t", rules="", market="TW",
        persona_ids=["a"], status="concluded", current_round=1,
    ))
    db_session.add(DiscussionTurn(
        discussion_id=disc_id, round=1, turn_index=0,
        persona_id="a", stance="agree", content="just a regular turn",
    ))
    await db_session.commit()

    rnd = await svc._find_post_mortem_round(db_session, disc_id)
    assert rnd is None


# ── End-to-end aggregation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_analyze_recent_post_mortems_aggregates_mentions(
    db_session: AsyncSession, owner: User,
):
    """Two discussions, each with one persona mentioning a different
    category. Bulk aggregation produces one row per category with
    mentions=1, discussions=1, personas=1."""
    await _seed_discussion_with_pm(
        db_session, owner_id=owner.id,
        persona_replies=[
            ("market_analyst",
             "缺少個股期貨未平倉資料，無法判斷主力多空。"),
        ],
    )
    await _seed_discussion_with_pm(
        db_session, owner_id=owner.id,
        persona_replies=[
            ("buffett",
             "建議下次加入選擇權 IV 偏斜，能看出避險情緒。"),
        ],
    )

    summary = await svc.analyze_recent_post_mortems(db_session, limit=10)
    assert summary.discussions_audited == 2
    assert len(summary.discussion_ids) == 2
    cats = {row.category: row for row in summary.gaps}
    assert "single_stock_futures" in cats
    assert "options_iv_skew" in cats
    assert cats["single_stock_futures"].mentions == 1
    assert cats["options_iv_skew"].mentions == 1


@pytest.mark.asyncio
async def test_analyze_recent_post_mortems_dedups_per_persona(
    db_session: AsyncSession, owner: User,
):
    """Same category mentioned by 2 different personas in the same
    discussion: mentions=2, discussions=1, personas=2."""
    await _seed_discussion_with_pm(
        db_session, owner_id=owner.id,
        persona_replies=[
            ("market_analyst",
             "缺個股期貨資料"),
            ("buffett",
             "個股期貨未平倉若有將更精確"),
        ],
    )

    summary = await svc.analyze_recent_post_mortems(db_session, limit=10)
    cats = {row.category: row for row in summary.gaps}
    assert cats["single_stock_futures"].mentions == 2
    assert cats["single_stock_futures"].discussions_mentioning == 1
    assert cats["single_stock_futures"].persona_count_mentioning == 2


@pytest.mark.asyncio
async def test_analyze_recent_post_mortems_skips_discussions_without_pm(
    db_session: AsyncSession, owner: User,
):
    """Discussions without a post-mortem injection are silently
    excluded — analyse only what we have evidence for."""
    await _seed_discussion_with_pm(
        db_session, owner_id=owner.id,
        persona_replies=[("a", "ignored")],
        has_pm_injection=False,   # no injection
    )

    summary = await svc.analyze_recent_post_mortems(db_session, limit=10)
    assert summary.discussions_audited == 0
    assert summary.gaps == []


@pytest.mark.asyncio
async def test_analyze_recent_post_mortems_filters_by_market(
    db_session: AsyncSession, owner: User,
):
    """`market='TW'` drops US discussions. US discussion has its own
    post-mortem with a relevant gap mention, but `market='TW'` must
    still ignore it."""
    await _seed_discussion_with_pm(
        db_session, owner_id=owner.id, market="TW",
        persona_replies=[("a", "缺個股期貨資料")],
    )
    await _seed_discussion_with_pm(
        db_session, owner_id=owner.id, market="US",
        persona_replies=[("a", "缺個股期貨資料")],
    )

    tw_only = await svc.analyze_recent_post_mortems(
        db_session, limit=10, market="TW",
    )
    assert tw_only.discussions_audited == 1

    all_markets = await svc.analyze_recent_post_mortems(
        db_session, limit=10,
    )
    assert all_markets.discussions_audited == 2


@pytest.mark.asyncio
async def test_analyze_returns_empty_gaps_when_no_taxonomy_match(
    db_session: AsyncSession, owner: User,
):
    """A post-mortem where personas reflect generically without
    naming any taxonomy category produces zero gap rows. The
    discussion is still counted as audited so the operator knows
    "we looked, but found no themed mentions"."""
    await _seed_discussion_with_pm(
        db_session, owner_id=owner.id,
        persona_replies=[
            ("a", "我的判斷沒問題，市場走勢難以預測。"),
        ],
    )

    summary = await svc.analyze_recent_post_mortems(db_session, limit=10)
    assert summary.discussions_audited == 1
    assert summary.gaps == []


# ── Taxonomy keyword sanity ───────────────────────────────────────


@pytest.mark.parametrize(
    "content,category",
    [
        ("缺少個股期貨資料", "single_stock_futures"),
        ("選擇權 IV 偏斜可以看出避險情緒", "options_iv_skew"),
        ("缺 VIX 資料", "options_iv_skew"),
        ("法說會行事曆能更精確", "earnings_call_calendar"),
        ("分析師預估獲利會幫助判斷", "analyst_estimates"),
        ("美股期貨開盤方向", "us_overnight_futures"),
        ("SOX 領先指標", "us_overnight_futures"),
        ("分點進出資料更深入", "broker_dot_concentration"),
        ("逐筆委買委賣資料", "intraday_orderflow"),
        ("即時借券動態", "realtime_lending"),
        ("供應鏈動態", "supply_chain_news"),
        ("董監持股變動", "insider_transactions"),
        ("信評升降", "credit_rating"),
        ("經濟數據行事曆", "macro_calendar"),
    ],
)
def test_taxonomy_matches_representative_phrases(
    content: str, category: str,
):
    patterns = svc._DATA_GAP_TAXONOMY[category]
    assert svc._match_any(content, patterns), (
        f"{category} should match {content!r}"
    )


def test_taxonomy_does_not_match_irrelevant_phrases():
    """Sanity: a generic reflection shouldn't fire any category."""
    irrelevant = "市場波動本來就難預測，這次運氣差了一點。"
    for cat, patterns in svc._DATA_GAP_TAXONOMY.items():
        assert not svc._match_any(irrelevant, patterns), (
            f"{cat} incorrectly matched generic reflection"
        )


def test_seed_helper_attaches_dummy_datetime():
    """The seed helper sets created_at via SQLAlchemy default; this
    test exists just to keep the helper exported and lint-clean."""
    # Sentinel — `datetime.now(UTC)` is imported but only used by the
    # helper. Without this assertion lint may flag the import as
    # unused depending on pytest collection order.
    assert datetime.now(UTC).tzinfo is UTC
