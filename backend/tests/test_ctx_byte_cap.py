"""G7-2a: per-block context byte cap.

`_apply_block_byte_caps` is the safety net that bounds pathological
context blowups (mainly `focus_briefs` when a topic names many
symbols). These tests pin the three behaviours that matter:

  - under-cap blocks are byte-for-byte untouched (the common path —
    this is what keeps signal_audit flat),
  - over-cap list blocks are trimmed to a fitting prefix + a sentinel
    recording the drop count,
  - non-list / empty / uncapped blocks are ignored.
"""
import json

from services.discussion.context.builder import (
    _BLOCK_BYTE_CAPS,
    _apply_block_byte_caps,
    _serialized_bytes,
)


def _brief(symbol: str, padding: int = 0) -> dict:
    """A focus-brief-shaped dict, optionally padded to inflate bytes."""
    b = {"symbol": symbol, "quote": {"price": 100.0}}
    if padding:
        b["_pad"] = "x" * padding
    return b


def test_under_cap_block_is_untouched():
    cap = _BLOCK_BYTE_CAPS["focus_briefs"]
    briefs = [_brief("2330"), _brief("2317")]
    assert _serialized_bytes(briefs) < cap
    ctx = {"focus_briefs": list(briefs)}

    _apply_block_byte_caps(ctx)

    # Byte-identical: no sentinel appended, order + contents preserved.
    assert ctx["focus_briefs"] == briefs


def test_over_cap_block_is_trimmed_with_sentinel():
    cap = _BLOCK_BYTE_CAPS["focus_briefs"]
    # Each brief ~ (cap / 4) bytes → 10 of them blow well past the cap.
    per = cap // 4
    briefs = [_brief(f"S{i:02d}", padding=per) for i in range(10)]
    ctx = {"focus_briefs": briefs}

    _apply_block_byte_caps(ctx)

    out = ctx["focus_briefs"]
    sentinel = out[-1]
    kept = out[:-1]
    # Sentinel records the drop and is clearly not a brief (no symbol).
    assert "symbol" not in sentinel
    assert sentinel["_omitted"] == len(briefs) - len(kept)
    assert sentinel["_omitted"] > 0
    # Kept prefix is contiguous from the front (relevance order preserved).
    assert [b["symbol"] for b in kept] == [
        f"S{i:02d}" for i in range(len(kept))
    ]
    # Precise invariant: the kept prefix (before the sentinel) is within
    # the byte budget.
    assert _serialized_bytes(kept) <= cap


def test_single_oversized_item_is_kept():
    """A lone brief bigger than the whole cap is still kept — the cap
    bounds the item *count*, dropping everything would lose more."""
    cap = _BLOCK_BYTE_CAPS["focus_briefs"]
    big = _brief("HUGE", padding=cap * 2)
    ctx = {"focus_briefs": [big]}

    _apply_block_byte_caps(ctx)

    # Only the one (oversized) brief, no sentinel (nothing was dropped).
    assert ctx["focus_briefs"] == [big]


def test_non_list_and_empty_blocks_ignored():
    ctx = {
        "focus_briefs": [],          # empty list
        "macro": {"x": "y" * 999},   # not a capped block
        "index": None,               # not a list
    }
    snapshot = json.dumps(ctx, sort_keys=True)

    _apply_block_byte_caps(ctx)

    assert json.dumps(ctx, sort_keys=True) == snapshot
