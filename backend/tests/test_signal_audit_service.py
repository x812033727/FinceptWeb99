"""Unit tests for `services.signal_audit_service`.

Covers the pure-compute pieces (presence detection, keyword
matching, audit aggregation) without touching the DB. The DB-bound
``audit_discussion`` is exercised via a small integration test that
seeds rows and walks the full path.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion, DiscussionTurn
from models.discussion_round_context import DiscussionRoundContext
from models.user import User, UserRole
from services import signal_audit_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"audit-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ── _detect_present_signals ───────────────────────────────────────


def test_detect_present_signals_picks_up_per_symbol_subfields():
    """Per-symbol short_term_signals fields surface as
    `short_term_signals.<sub_key>` so coverage rolls up across all
    focus symbols regardless of which ones held data."""
    ctx = {
        "short_term_signals": {
            "2330": {
                "volume_ratio": 2.3,
                "rsi_14": 67.0,
                "industry_rs": {"rs_score": 1.2},
                "day_trading_trend": None,   # absent
                "upcoming_event": None,      # absent
            },
        },
    }
    present = svc._detect_present_signals(ctx)
    assert "short_term_signals.volume_ratio" in present
    assert "short_term_signals.rsi_14" in present
    assert "short_term_signals.industry_rs" in present
    assert "short_term_signals.day_trading_trend" not in present
    assert "short_term_signals.upcoming_event" not in present


def test_detect_present_signals_picks_top_level_blocks():
    ctx = {
        "taifex_positioning": {"trend": "bearish"},
        "news_sentiment": {"avg_score": 0.1},
        "macro": None,                 # absent
        "active_buybacks": [],          # empty list = absent
        "top_foreign_buyers": [{"symbol": "2330", "buy": 100}],
    }
    present = svc._detect_present_signals(ctx)
    assert "taifex_positioning" in present
    assert "news_sentiment" in present
    assert "top_foreign_buyers" in present
    assert "macro" not in present
    assert "active_buybacks" not in present


def test_detect_present_signals_handles_empty_context():
    assert svc._detect_present_signals({}) == set()
    assert svc._detect_present_signals({"short_term_signals": {}}) == set()


# ── _match_keywords ───────────────────────────────────────────────


@pytest.mark.parametrize("content,signal,expected", [
    # Chinese tokens
    ("外資台指期多單明顯減少",     "taifex_positioning",                    True),
    ("RSI 已逼近 70 屬超買區",     "short_term_signals.rsi_14",             True),
    ("KD 出現黃金交叉",            "short_term_signals.kd_k",               True),
    ("量能放大為 5 日均量 2.3 倍",  "short_term_signals.volume_ratio",       True),
    ("近五日漲幅 4%",              "short_term_signals.return_5d",          True),
    ("今日開盤跳空向上",            "short_term_signals.gap_pct",            True),
    ("領先半導體類股",              "short_term_signals.industry_rs",        True),
    ("當沖比連 5 日攀升",           "short_term_signals.day_trading_trend",  True),
    ("借券餘額大增",                "short_term_signals.securities_lending_trend", True),
    ("3 日後法說會",                "short_term_signals.upcoming_event",     True),
    # English tokens
    ("RSI is overbought",          "short_term_signals.rsi_14",             True),
    ("upcoming earnings call",     "short_term_signals.upcoming_event",     True),
    # Negative cases
    ("純技術面分析無相關",          "taifex_positioning",                    False),
    ("價格走勢平穩",                "short_term_signals.rsi_14",             False),
])
def test_match_keywords_per_signal(content, signal, expected):
    patterns = svc._SIGNAL_KEYWORDS[signal]
    assert svc._match_keywords(content, patterns) is expected


# ── _has_value_near_keyword (PR #258 value-citation upgrade) ──────


@pytest.mark.parametrize("content,patterns,expected", [
    # Concrete value next to keyword → True
    ("RSI 67.3 已逼近超買區",     ["RSI"],          True),
    ("外資台指期 +3500 口淨空",   ["外資台指期"],   True),
    ("量能放大為均量 2.3 倍",      ["量能"],         True),
    ("RSI 67",                     ["RSI"],          True),   # int still counts
    # Keyword without nearby number → False
    ("RSI 偏高",                   ["RSI"],          False),
    ("外資動向轉弱",               ["外資台指期"],   False),
    ("量能放大",                   ["量能"],         False),
    # Number outside the proximity window → False
    # ("70" appears > 60 chars away from "RSI" — out of window)
    (
        "RSI 處於中性區。" + "X" * 80 + " 此外，目標價 70。",
        ["RSI"],
        False,
    ),
    # Multiple keyword matches; only one needs a nearby number
    ("RSI 偏高，但稍後 RSI 67.3 跌破", ["RSI"], True),
])
def test_has_value_near_keyword(content, patterns, expected):
    assert svc._has_value_near_keyword(content, patterns) is expected


def test_has_value_recognises_unit_suffixes():
    """zh-TW writing often has unit-suffixed numbers like `+3500口`,
    `25%`, `1.2倍`. The numeric token regex covers each — pin them
    so a future `_NUMERIC_TOKEN` tweak doesn't accidentally drop a
    common citation form."""
    assert svc._has_value_near_keyword("外資 +3500口", ["外資"]) is True
    assert svc._has_value_near_keyword("漲幅 5.2%", ["漲幅"]) is True
    assert svc._has_value_near_keyword("量能 1.2 倍", ["量能"]) is True
    assert svc._has_value_near_keyword("淨買 100 張", ["淨買"]) is True


# ── audit_turn ────────────────────────────────────────────────────


def test_audit_turn_only_checks_present_signals():
    """Signal NOT in `signals_present` is excluded from `cited` —
    it goes through the `hallucinated` path instead (PR #259)."""
    audit = svc.audit_turn(
        round_no=1,
        persona_id="market_analyst",
        stance="agree",
        content="量能 2.3x、RSI 67、外資台指期淨空增加",
        signals_present={
            "short_term_signals.volume_ratio",
            "taifex_positioning",
            # short_term_signals.rsi_14 NOT in present → checked
            # via the hallucination path, not `cited`.
        },
    )
    assert audit.cited_count == 2
    assert audit.cited["short_term_signals.volume_ratio"] is True
    assert audit.cited["taifex_positioning"] is True
    assert "short_term_signals.rsi_14" not in audit.cited


def test_audit_turn_zero_citation_for_irrelevant_content():
    audit = svc.audit_turn(
        round_no=1,
        persona_id="buffett",
        stance="dissent",
        content="長期持有優質公司比短線進出有效",
        signals_present={
            "short_term_signals.volume_ratio",
            "short_term_signals.rsi_14",
        },
    )
    assert audit.cited_count == 0
    assert audit.cited_with_value_count == 0


def test_audit_turn_distinguishes_name_drop_from_value_citation():
    """Pin the PR #258 contract: name-drop ("RSI 偏高") gets
    `cited=True, cited_with_value=False`. Concrete-value citation
    ("RSI 67.3 已逼近超買") gets BOTH true. The two flags are the
    operator's lever for spotting "personas mention but don't
    leverage" patterns."""
    # Space the keywords ≥ 60 chars apart so the numeric token next
    # to RSI doesn't leak into the proximity windows of the other
    # keywords (the helper scans ±60 chars per keyword match).
    spacer = "—" * 80   # > 60 chars of filler with no digits
    audit = svc.audit_turn(
        round_no=1,
        persona_id="market_analyst",
        stance="agree",
        content=(
            f"RSI 67.3 已逼近超買區。{spacer}"
            f"量能放大但缺乏數據說服力。{spacer}"
            f"外資買超並未停止。"
        ),
        signals_present={
            "short_term_signals.rsi_14",       # cited WITH value (67.3)
            "short_term_signals.volume_ratio", # cited but NO value
            "top_foreign_buyers",              # cited but NO value
        },
    )
    # All three keyword-cited.
    assert audit.cited["short_term_signals.rsi_14"] is True
    assert audit.cited["short_term_signals.volume_ratio"] is True
    assert audit.cited["top_foreign_buyers"] is True
    # Only RSI has a value within ±60 chars of the keyword.
    assert audit.cited_with_value["short_term_signals.rsi_14"] is True
    assert audit.cited_with_value["short_term_signals.volume_ratio"] is False
    assert audit.cited_with_value["top_foreign_buyers"] is False
    assert audit.cited_count == 3
    assert audit.cited_with_value_count == 1


# ── PR #259: hallucination detection ──────────────────────────────


def test_audit_turn_flags_hallucination_when_signal_absent_with_value():
    """Persona quotes a numeric value for a signal that wasn't in the
    round prompt → `hallucinated[sig]=True`. The numeric requirement
    keeps general-knowledge mentions out of the count."""
    spacer = "—" * 80   # keep "67.3" out of taifex's ±60 window
    audit = svc.audit_turn(
        round_no=1,
        persona_id="market_analyst",
        stance="agree",
        # RSI cited WITH value but not in signals_present →
        # hallucination. taifex cited without value → not flagged.
        content=(
            f"RSI 67.3 已逼近超買區。{spacer}外資台指期未平倉值得關注。"
        ),
        signals_present={"short_term_signals.volume_ratio"},
    )
    assert audit.hallucinated["short_term_signals.rsi_14"] is True
    assert audit.hallucinated["taifex_positioning"] is False
    assert audit.hallucinated_count == 1
    # Hallucinated signals don't double-count in `cited`.
    assert "short_term_signals.rsi_14" not in audit.cited


def test_audit_turn_no_hallucination_for_namedrop_without_value():
    """Mentioning "RSI 偏高" without a number is treated as general
    knowledge / paraphrasing, not fabrication. The metric requires
    a numeric token to keep noise low."""
    audit = svc.audit_turn(
        round_no=1,
        persona_id="market_analyst",
        stance="agree",
        content="RSI 偏高，外資未平倉值得觀察。",
        signals_present={"short_term_signals.volume_ratio"},
    )
    assert audit.hallucinated["short_term_signals.rsi_14"] is False
    assert audit.hallucinated_count == 0


def test_audit_turn_present_signal_never_in_hallucinated_dict():
    """Mutual exclusion: a signal in `signals_present` never appears
    in `hallucinated` (avoids double-counting)."""
    audit = svc.audit_turn(
        round_no=1,
        persona_id="market_analyst",
        stance="agree",
        content="RSI 67.3 已逼近超買區。",
        signals_present={"short_term_signals.rsi_14"},
    )
    assert audit.cited["short_term_signals.rsi_14"] is True
    assert audit.cited_with_value["short_term_signals.rsi_14"] is True
    assert "short_term_signals.rsi_14" not in audit.hallucinated


def test_discussion_audit_hallucinations_aggregates_across_rounds():
    """Two rounds where RSI is absent in one and present in the other.
    The aggregate must count hallucinations only against the absent
    rounds; the present round contributes 0 to the denominator."""
    audit = svc.DiscussionAudit(
        discussion_id=str(uuid4()), topic="test",
    )
    # Round 1: RSI present → not a hallucination opportunity.
    r1 = svc.RoundAudit(round=1, signals_present={
        "short_term_signals.rsi_14",
    })
    r1.turns = [
        svc.TurnAudit(
            round=1, persona_id="a", stance="agree",
            cited={"short_term_signals.rsi_14": True},
            cited_with_value={"short_term_signals.rsi_14": True},
            hallucinated={},
        ),
    ]
    # Round 2: RSI absent → eligible. 2 personas, 1 hallucinates.
    r2 = svc.RoundAudit(round=2, signals_present={"taifex_positioning"})
    r2.turns = [
        svc.TurnAudit(
            round=2, persona_id="a", stance="agree",
            hallucinated={"short_term_signals.rsi_14": True},
        ),
        svc.TurnAudit(
            round=2, persona_id="b", stance="dissent",
            hallucinated={"short_term_signals.rsi_14": False},
        ),
    ]
    audit.rounds = [r1, r2]

    hall = audit.hallucinations()
    rsi = hall["short_term_signals.rsi_14"]
    assert rsi["absent_rounds"] == 1                # only round 2
    assert rsi["persona_count_absent"] == 2         # 2 personas in round 2
    assert rsi["hallucinated"] == 1                 # only persona "a"


def test_discussion_audit_hallucinations_omits_always_present_signals():
    """A signal that's in `signals_present` for every round can't be
    hallucinated relative to this discussion — denominator is 0."""
    audit = svc.DiscussionAudit(
        discussion_id=str(uuid4()), topic="test",
    )
    r1 = svc.RoundAudit(round=1, signals_present={
        "short_term_signals.rsi_14",
    })
    r1.turns = [
        svc.TurnAudit(round=1, persona_id="a", stance="agree"),
    ]
    audit.rounds = [r1]

    hall = audit.hallucinations()
    # Signal was in every round → either omitted or zero across the board.
    rsi_stats = hall.get("short_term_signals.rsi_14", {
        "absent_rounds": 0, "hallucinated": 0, "persona_count_absent": 0,
    })
    assert rsi_stats["absent_rounds"] == 0
    assert rsi_stats["hallucinated"] == 0
    assert rsi_stats["persona_count_absent"] == 0


# ── DiscussionAudit.coverage ──────────────────────────────────────


def test_coverage_aggregates_across_rounds_and_personas():
    audit = svc.DiscussionAudit(
        discussion_id=str(uuid4()), topic="test",
    )
    # Round 1: 3 personas, RSI present, 2 cite it.
    r1 = svc.RoundAudit(round=1, signals_present={
        "short_term_signals.rsi_14", "taifex_positioning",
    })
    r1.turns = [
        svc.TurnAudit(round=1, persona_id="a", stance="agree", cited={
            "short_term_signals.rsi_14": True, "taifex_positioning": False,
        }),
        svc.TurnAudit(round=1, persona_id="b", stance="agree", cited={
            "short_term_signals.rsi_14": True, "taifex_positioning": False,
        }),
        svc.TurnAudit(round=1, persona_id="c", stance="dissent", cited={
            "short_term_signals.rsi_14": False, "taifex_positioning": True,
        }),
    ]
    # Round 2: 2 personas, only taifex in context, 1 cites it.
    r2 = svc.RoundAudit(round=2, signals_present={"taifex_positioning"})
    r2.turns = [
        svc.TurnAudit(round=2, persona_id="a", stance="agree", cited={
            "taifex_positioning": True,
        }),
        svc.TurnAudit(round=2, persona_id="b", stance="agree", cited={
            "taifex_positioning": False,
        }),
    ]
    audit.rounds = [r1, r2]

    cov = audit.coverage()
    # RSI: present in 1 round × 3 personas = 3 pairs; 2 cited.
    assert cov["short_term_signals.rsi_14"]["persona_count"] == 3
    assert cov["short_term_signals.rsi_14"]["cited"] == 2
    assert cov["short_term_signals.rsi_14"]["present"] == 1
    # taifex: present in 2 rounds × (3+2) = 5 pairs; 2 cited (1 each round).
    assert cov["taifex_positioning"]["persona_count"] == 5
    assert cov["taifex_positioning"]["cited"] == 2
    assert cov["taifex_positioning"]["present"] == 2


# ── DB-bound audit_discussion ─────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_discussion_returns_none_for_missing_id(
    db_session: AsyncSession,
):
    out = await svc.audit_discussion(db_session, uuid4())
    assert out is None


@pytest.mark.asyncio
async def test_audit_discussion_returns_none_when_no_round_contexts(
    db_session: AsyncSession, owner: User,
):
    """Older auto-run rows from before the round-context column
    landed have no `discussion_round_contexts` entries — caller
    can branch on None to surface a cleaner error message."""
    disc_id = uuid4()
    db_session.add(Discussion(
        id=disc_id, owner_id=owner.id, topic="test", rules="",
        market="TW", persona_ids=["market_analyst"],
        status="concluded", current_round=0,
    ))
    await db_session.commit()

    out = await svc.audit_discussion(db_session, disc_id)
    assert out is None


@pytest.mark.asyncio
async def test_audit_discussion_walks_full_path(
    db_session: AsyncSession, owner: User,
):
    """Seed one round with two persona turns, one citing RSI + the
    taifex block, the other ignoring both. End-to-end check that
    presence detection + keyword matching + aggregation produces the
    expected coverage table."""
    disc_id = uuid4()
    db_session.add(Discussion(
        id=disc_id, owner_id=owner.id, topic="2330 短線分析",
        rules="", market="TW",
        persona_ids=["market_analyst", "buffett"],
        status="concluded", current_round=1,
    ))
    db_session.add(DiscussionRoundContext(
        discussion_id=disc_id, round=1,
        context={
            "short_term_signals": {
                "2330": {
                    "rsi_14": 67.0,
                    "volume_ratio": 2.3,
                    "industry_rs": {"rs_score": 1.2},
                },
            },
            "taifex_positioning": {"trend": "bearish"},
        },
        captured_at=datetime.now(UTC),
    ))
    db_session.add_all([
        DiscussionTurn(
            discussion_id=disc_id, round=1, turn_index=0,
            persona_id="market_analyst", stance="agree",
            content="RSI 67 已逼近超買，量能放大為均量 2.3 倍，"
                    "領先類股。外資台指期淨空增加，短線方向偏空。",
        ),
        DiscussionTurn(
            discussion_id=disc_id, round=1, turn_index=1,
            persona_id="buffett", stance="dissent",
            content="技術指標短線雜訊太多，看長期持有護城河寬度即可。",
        ),
    ])
    await db_session.commit()

    audit = await svc.audit_discussion(db_session, disc_id)
    assert audit is not None
    assert audit.topic == "2330 短線分析"
    assert len(audit.rounds) == 1
    r = audit.rounds[0]
    assert "short_term_signals.rsi_14" in r.signals_present
    assert "taifex_positioning" in r.signals_present

    by_persona = {t.persona_id: t for t in r.turns}
    # market_analyst cited RSI / volume_ratio / industry_rs / taifex
    assert by_persona["market_analyst"].cited["short_term_signals.rsi_14"] is True
    assert by_persona["market_analyst"].cited["short_term_signals.volume_ratio"] is True
    assert by_persona["market_analyst"].cited["taifex_positioning"] is True
    # buffett cited none
    assert by_persona["buffett"].cited_count == 0

    # Coverage: market_analyst cites all three → 3/2-pair = 1.5 cited
    # for each of three signals. Concretely: RSI present in 1 round ×
    # 2 personas = 2 pairs; 1 cited. industry_rs same shape.
    cov = audit.coverage()
    assert cov["short_term_signals.rsi_14"]["cited"] == 1
    assert cov["short_term_signals.rsi_14"]["persona_count"] == 2
    assert cov["taifex_positioning"]["cited"] == 1
    assert cov["taifex_positioning"]["persona_count"] == 2


# ── audit_recent_discussions (bulk) ───────────────────────────────


async def _seed_concluded_discussion(
    db_session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    topic: str,
    market: str = "TW",
    cite_rsi: bool,
) -> uuid.UUID:
    """Helper: insert one concluded discussion with a single round
    context + one persona turn whose content does/doesn't mention RSI."""
    disc_id = uuid4()
    db_session.add(Discussion(
        id=disc_id, owner_id=owner_id, topic=topic,
        rules="", market=market,
        persona_ids=["market_analyst"],
        status="concluded", current_round=1,
    ))
    db_session.add(DiscussionRoundContext(
        discussion_id=disc_id, round=1,
        context={
            "short_term_signals": {
                "2330": {"rsi_14": 67.0, "volume_ratio": 2.3},
            },
        },
        captured_at=datetime.now(UTC),
    ))
    content = (
        "RSI 67 已逼近超買，量能放大為均量 2.3 倍。"
        if cite_rsi
        else "純粹從基本面看公司的長期競爭優勢。"
    )
    db_session.add(DiscussionTurn(
        discussion_id=disc_id, round=1, turn_index=0,
        persona_id="market_analyst", stance="agree",
        content=content,
    ))
    await db_session.commit()
    return disc_id


@pytest.mark.asyncio
async def test_audit_recent_discussions_returns_zero_when_archive_empty(
    db_session: AsyncSession,
):
    summary = await svc.audit_recent_discussions(db_session, limit=10)
    assert summary.discussions_audited == 0
    assert summary.discussion_ids == []
    assert summary.coverage == {}


@pytest.mark.asyncio
async def test_audit_recent_discussions_aggregates_citation_stats(
    db_session: AsyncSession, owner: User,
):
    """Seed 3 concluded discussions where 2 cite RSI and 1 doesn't.
    Bulk audit must produce: present=3, cited=2, persona_count=3
    (one persona per discussion, all see RSI in context)."""
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id, topic="d1", cite_rsi=True,
    )
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id, topic="d2", cite_rsi=True,
    )
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id, topic="d3", cite_rsi=False,
    )

    summary = await svc.audit_recent_discussions(db_session, limit=10)
    assert summary.discussions_audited == 3
    assert len(summary.discussion_ids) == 3
    rsi = summary.coverage["short_term_signals.rsi_14"]
    assert rsi["present"] == 3        # one round per discussion
    assert rsi["persona_count"] == 3   # one persona × three discussions
    assert rsi["cited"] == 2           # two of three cited


@pytest.mark.asyncio
async def test_audit_recent_discussions_filters_by_market(
    db_session: AsyncSession, owner: User,
):
    """`market='TW'` must drop non-TW discussions from the roll-up."""
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id, topic="tw1",
        market="TW", cite_rsi=True,
    )
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id, topic="us1",
        market="US", cite_rsi=True,
    )

    tw_only = await svc.audit_recent_discussions(
        db_session, limit=10, market="TW",
    )
    assert tw_only.discussions_audited == 1

    all_markets = await svc.audit_recent_discussions(
        db_session, limit=10,
    )
    assert all_markets.discussions_audited == 2


@pytest.mark.asyncio
async def test_audit_recent_discussions_carries_hallucinations(
    db_session: AsyncSession, owner: User,
):
    """PR #259: bulk roll-up surfaces signals personas cite with values
    despite the round prompt not containing them. Seed a discussion
    where the context only has volume_ratio but the persona quotes
    `RSI 67.3` — that's a hallucination."""
    disc_id = uuid4()
    db_session.add(Discussion(
        id=disc_id, owner_id=owner.id, topic="hall",
        rules="", market="TW",
        persona_ids=["market_analyst"],
        status="concluded", current_round=1,
    ))
    db_session.add(DiscussionRoundContext(
        discussion_id=disc_id, round=1,
        # Only volume_ratio present — RSI is absent from prompt.
        context={
            "short_term_signals": {
                "2330": {"volume_ratio": 2.3},
            },
        },
        captured_at=datetime.now(UTC),
    ))
    spacer = "—" * 80   # keep "67.3" out of gap_pct's ±60 window
    db_session.add(DiscussionTurn(
        discussion_id=disc_id, round=1, turn_index=0,
        persona_id="market_analyst", stance="agree",
        # Hallucinates RSI 67.3 (with value) and an absent gap_pct (no
        # value, so not flagged) for sanity.
        content=(
            f"RSI 67.3 已逼近超買區。{spacer}"
            f"開盤跳空缺口仍待觀察。"
        ),
    ))
    await db_session.commit()

    summary = await svc.audit_recent_discussions(db_session, limit=10)
    assert summary.discussions_audited == 1
    rsi = summary.hallucinations["short_term_signals.rsi_14"]
    assert rsi["absent_rounds"] == 1
    assert rsi["persona_count_absent"] == 1
    assert rsi["hallucinated"] == 1
    # gap_pct mentioned without a value → not hallucinated.
    gap = summary.hallucinations.get("short_term_signals.gap_pct", {})
    assert gap.get("hallucinated", 0) == 0


# ── PR #263: snapshot persistence + sparkline reads ──────────────


@pytest.mark.asyncio
async def test_snapshot_audit_to_history_writes_one_row_per_signal(
    db_session: AsyncSession, owner: User,
):
    """A bulk audit produces one row per signal in
    signal_audit_history. Each row carries the matching counters."""
    from datetime import date as _date

    from models.signal_audit_history import SignalAuditHistory

    # Seed one discussion that cites RSI.
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id, topic="snap1", cite_rsi=True,
    )
    when = _date(2026, 5, 3)

    written = await svc.snapshot_audit_to_history(
        db_session, captured_at=when, market="TW",
    )
    assert written > 0

    rsi = (await db_session.scalars(
        select(SignalAuditHistory).where(
            SignalAuditHistory.signal == "short_term_signals.rsi_14",
            SignalAuditHistory.captured_at == when,
            SignalAuditHistory.market == "TW",
        )
    )).first()
    assert rsi is not None
    assert rsi.cited_count == 1
    assert rsi.persona_count == 1
    assert rsi.discussions_audited == 1


@pytest.mark.asyncio
async def test_snapshot_audit_to_history_is_idempotent(
    db_session: AsyncSession, owner: User,
):
    """Re-running the snapshot on the same UTC day overwrites in
    place — the (captured_at, market, signal) PK + upsert
    guarantees no duplicate rows."""
    from datetime import date as _date

    from models.signal_audit_history import SignalAuditHistory

    await _seed_concluded_discussion(
        db_session, owner_id=owner.id, topic="idem1", cite_rsi=True,
    )
    when = _date(2026, 5, 3)

    await svc.snapshot_audit_to_history(
        db_session, captured_at=when, market="TW",
    )
    first_count = await db_session.scalar(
        select(func.count()).select_from(SignalAuditHistory).where(
            SignalAuditHistory.captured_at == when,
        )
    )

    # Run again — same data, same day.
    await svc.snapshot_audit_to_history(
        db_session, captured_at=when, market="TW",
    )
    second_count = await db_session.scalar(
        select(func.count()).select_from(SignalAuditHistory).where(
            SignalAuditHistory.captured_at == when,
        )
    )
    assert second_count == first_count, (
        "second run must overwrite, not append"
    )


@pytest.mark.asyncio
async def test_read_signal_history_returns_sorted_series(
    db_session: AsyncSession,
):
    """Series ordered by ascending captured_at so the frontend can
    plot left-to-right without reversing."""
    from datetime import datetime as _dt, timedelta as _td

    from models.signal_audit_history import SignalAuditHistory
    today = _dt.utcnow().date()

    # Seed three days of snapshots for one signal — anchored at
    # today minus offsets so the read window catches them.
    for offset, cited in [(2, 1), (1, 2), (0, 4)]:
        db_session.add(SignalAuditHistory(
            captured_at=today - _td(days=offset),
            market="TW",
            signal="short_term_signals.rsi_14",
            discussions_audited=10,
            present_count=10, cited_count=cited,
            cited_with_value_count=0, hallucinated_count=0,
            persona_count=10, persona_count_absent=0,
        ))
    await db_session.commit()

    points = await svc.read_signal_history(
        db_session,
        signal="short_term_signals.rsi_14",
        market="TW",
        days=10,
    )
    assert len(points) == 3
    # Ascending — earliest day first.
    assert points[0]["cited_count"] == 1
    assert points[2]["cited_count"] == 4
    # Pre-computed citation_rate.
    assert points[2]["citation_rate"] == 0.4


@pytest.mark.asyncio
async def test_read_signal_history_filters_by_market(
    db_session: AsyncSession,
):
    """`market='TW'` excludes US rows. NULL market filter (all-markets
    aggregate) is a separate query — not implicit in 'TW'."""
    from datetime import datetime as _dt

    from models.signal_audit_history import SignalAuditHistory
    today = _dt.utcnow().date()

    db_session.add(SignalAuditHistory(
        captured_at=today, market="TW",
        signal="short_term_signals.rsi_14",
        discussions_audited=1, present_count=1, cited_count=1,
        cited_with_value_count=0, hallucinated_count=0,
        persona_count=1, persona_count_absent=0,
    ))
    db_session.add(SignalAuditHistory(
        captured_at=today, market="US",
        signal="short_term_signals.rsi_14",
        discussions_audited=1, present_count=1, cited_count=0,
        cited_with_value_count=0, hallucinated_count=0,
        persona_count=1, persona_count_absent=0,
    ))
    await db_session.commit()

    tw = await svc.read_signal_history(
        db_session, signal="short_term_signals.rsi_14",
        market="TW", days=10,
    )
    assert len(tw) == 1
    assert tw[0]["cited_count"] == 1


@pytest.mark.asyncio
async def test_read_all_signals_history_groups_by_signal(
    db_session: AsyncSession,
):
    """Bulk read returns `{signal: [points...]}`. Different signals
    each get their own list, points within each list are sorted
    ascending."""
    from models.signal_audit_history import SignalAuditHistory
    from datetime import datetime as _dt, timedelta as _td
    today = _dt.utcnow().date()

    for sig, cited in [
        ("short_term_signals.rsi_14", 5),
        ("short_term_signals.volume_ratio", 3),
    ]:
        for offset in (0, 1):
            db_session.add(SignalAuditHistory(
                captured_at=today - _td(days=offset),
                market="TW",
                signal=sig,
                discussions_audited=10,
                present_count=10, cited_count=cited,
                cited_with_value_count=0, hallucinated_count=0,
                persona_count=10, persona_count_absent=0,
            ))
    await db_session.commit()

    bulk = await svc.read_all_signals_history(
        db_session, market="TW", days=10,
    )
    assert "short_term_signals.rsi_14" in bulk
    assert "short_term_signals.volume_ratio" in bulk
    assert len(bulk["short_term_signals.rsi_14"]) == 2
    # Each series sorted by captured_at ascending.
    rsi = bulk["short_term_signals.rsi_14"]
    assert rsi[0]["captured_at"] <= rsi[1]["captured_at"]


@pytest.mark.asyncio
async def test_audit_recent_discussions_respects_limit(
    db_session: AsyncSession, owner: User,
):
    """`limit=N` clamps the number of discussions audited regardless
    of how many concluded rows exist. Don't assert which specific
    ones get picked — SQLite's `created_at` resolves at the second,
    so 3 rows seeded in the same test tick can share a timestamp
    and the descending sort isn't deterministic. The contract this
    test guards is just the COUNT, which is what callers rely on
    for budget control on large archives."""
    for topic in ("d1", "d2", "d3"):
        await _seed_concluded_discussion(
            db_session, owner_id=owner.id, topic=topic, cite_rsi=True,
        )

    summary = await svc.audit_recent_discussions(db_session, limit=2)
    assert summary.discussions_audited == 2
    assert len(summary.discussion_ids) == 2
