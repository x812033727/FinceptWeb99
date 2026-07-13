"""R6 PR2 digest consumption: `_format_history(prior_turns, round_digests)`.

The digest map is opt-in. These pin the two behaviours that matter:
  - `round_digests=None` (the default, and what the loop passes whenever
    the feature is off) is byte-identical to the pre-feature output,
  - when a digest exists for an OLDER round, that round renders as one
    digest line instead of a bag of per-turn one-liners; rounds without
    a digest still fall back to per-turn summaries.
"""
from types import SimpleNamespace

from services.discussion.transcript_format import _format_history


def _t(pid: str, rnd: int, content: str, stance: str = "bullish"):
    return SimpleNamespace(
        persona_id=pid, round=rnd, stance=stance, content=content,
    )


def _turns():
    # 12 turns: round 1 has 8, round 2 has 4. With _FULL_HISTORY_TURNS=6
    # the 6 oldest (all round 1) fall into the "older" (summary) bucket,
    # so round 1 is fully eligible for digest substitution.
    return (
        [_t(f"p{i}", 1, f"第一輪觀點 {i}") for i in range(8)]
        + [_t(f"q{i}", 2, f"第二輪觀點 {i}") for i in range(4)]
    )


def test_none_is_byte_identical_to_default():
    turns = _turns()
    assert _format_history(turns) == _format_history(turns, None)


def test_off_path_has_no_digest_line():
    out = _format_history(_turns(), None)
    assert "（較早輪次摘要）" in out          # older section still present
    assert "第1輪摘要：" not in out          # but no round-digest line


def test_older_round_rendered_as_single_digest():
    out = _format_history(_turns(), {1: "R1 共識未定：多空對立"})
    # Round 1 collapses to exactly one digest line...
    assert "- 第1輪摘要：R1 共識未定：多空對立" in out
    assert out.count("第1輪摘要") == 1
    # ...replacing the per-turn one-liners for that round's older turns.
    assert "第一輪觀點 0" not in out.split("（最近發言全文）")[0]


def test_round_without_digest_falls_back_to_per_turn():
    # A digest for a round that isn't in the older bucket (round 2 is all
    # in the recent full window) → older round 1 keeps per-turn summaries.
    out = _format_history(_turns(), {2: "R2 摘要"})
    assert "第1輪摘要" not in out
    assert "（較早輪次摘要）" in out
