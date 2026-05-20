"""Tests for services.email_service.

Two layers:
- `is_configured()` — fail-closed gate on SMTP_HOST + SMTP_FROM. Must
  surface False when either is missing so the auto-run cron skips
  silently in dev / unconfigured environments.
- `render_discussion_report_markdown()` — pure renderer over the
  Discussion + DiscussionTurn shape, exercised with a representative
  conclusion JSON (recommended_symbols / reasoning / risks / per-
  symbol recommendations).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

from models.discussion import Discussion, DiscussionTurn
from services import email_service


def test_is_configured_requires_host_and_from():
    with patch.object(email_service.settings, "SMTP_HOST", ""), \
         patch.object(email_service.settings, "SMTP_FROM", "x@example.com"):
        assert email_service.is_configured() is False

    with patch.object(email_service.settings, "SMTP_HOST", "smtp.example.com"), \
         patch.object(email_service.settings, "SMTP_FROM", ""):
        assert email_service.is_configured() is False

    with patch.object(email_service.settings, "SMTP_HOST", "smtp.example.com"), \
         patch.object(email_service.settings, "SMTP_FROM", "x@example.com"):
        assert email_service.is_configured() is True


def _make_discussion() -> Discussion:
    return Discussion(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        topic="本週台股短線進場機會",
        rules="每位專家 ≤ 200 字；必須引用數據。",
        persona_ids=["buffett", "lynch"],
        market="TW",
        status="completed",
        current_round=2,
        created_at=datetime(2026, 5, 19, 4, 0, tzinfo=UTC),
    )


def test_render_markdown_contains_core_sections():
    d = _make_discussion()
    conclusion = {
        "recommended_symbols": ["2330", "2454"],
        "reasoning": "AI 需求復甦 + 高速運算佔比上升。",
        "risks": ["匯率波動", "下游庫存"],
        "time_horizon": "short_term",
        "consensus_score": 0.78,
        "recommendations": [
            {"symbol": "2330", "confidence": 0.85, "reasoning": "先進製程領先"},
            {"symbol": "2454", "confidence": 0.6, "calibrated_confidence": 0.55,
             "reasoning": "5G 升級週期"},
        ],
    }
    turns = [
        DiscussionTurn(
            id=1, discussion_id=d.id, round=1, turn_index=0,
            persona_id="buffett", stance="agree",
            content="優質公司應該長期持有。",
            created_at=datetime(2026, 5, 19, 4, 1, tzinfo=UTC),
        ),
        DiscussionTurn(
            id=2, discussion_id=d.id, round=1, turn_index=1,
            persona_id="lynch", stance="supplement",
            content="留意成長率變化。",
            created_at=datetime(2026, 5, 19, 4, 2, tzinfo=UTC),
        ),
    ]

    md = email_service.render_discussion_report_markdown(
        d, conclusion, turns,
        persona_name={"buffett": "Warren Buffett", "lynch": "Peter Lynch"},
    )

    assert "每日專家圓桌討論" in md
    assert "2026-05-19" in md
    assert "本週台股短線進場機會" in md
    assert "TW" in md
    assert "Warren Buffett" in md
    assert "Peter Lynch" in md
    assert "## 結論" in md
    assert "2330, 2454" in md
    assert "short_term" in md
    assert "AI 需求復甦" in md
    assert "匯率波動" in md
    # Per-symbol recommendations table
    assert "| 標的 |" in md
    assert "85.0%" in md
    # Calibrated confidence column shows '—' when absent
    assert "—" in md
    # Round headings + turn content
    assert "### 第 1 輪" in md
    assert "優質公司應該長期持有" in md


def test_render_markdown_handles_missing_conclusion():
    d = _make_discussion()
    md = email_service.render_discussion_report_markdown(d, None, [])
    assert "未產生結論" in md
    # Still includes the topic + rules + persona block so the recipient
    # can see what was attempted.
    assert d.topic in md


def test_render_markdown_sorts_turns_by_round_then_turn_index():
    """Caller may pass turns in any order; renderer must group by
    round and order within a round by turn_index. Regression guard
    against accidentally rendering round 2 before round 1."""
    d = _make_discussion()
    turns = [
        DiscussionTurn(
            id=3, discussion_id=d.id, round=2, turn_index=0,
            persona_id="lynch", stance="agree", content="round-2-A",
            created_at=datetime.now(UTC),
        ),
        DiscussionTurn(
            id=1, discussion_id=d.id, round=1, turn_index=1,
            persona_id="lynch", stance="agree", content="round-1-B",
            created_at=datetime.now(UTC),
        ),
        DiscussionTurn(
            id=2, discussion_id=d.id, round=1, turn_index=0,
            persona_id="buffett", stance="agree", content="round-1-A",
            created_at=datetime.now(UTC),
        ),
    ]
    md = email_service.render_discussion_report_markdown(d, {}, turns)
    # Order of substrings inside the final string.
    pos = [md.index(x) for x in ("round-1-A", "round-1-B", "round-2-A")]
    assert pos == sorted(pos)
