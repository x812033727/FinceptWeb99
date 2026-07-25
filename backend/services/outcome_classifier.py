"""Single source of truth for discussion win/loss classification.

The previous design had three independent classifiers (the verifier
task, the post-mortem service, the Brier scoreboard) each
reimplementing thresholds and sampling. They drifted: a +4% close
could be `verdict=loss` from the verifier (3% high bar) while
`outcome_binary=0` from the Brier (5% close bar) AND
`status=marginal_win` from the post-mortem (3-5% band) — three
different answers for the same outcome.

This module replaces all three with one pure function over
`(d1_buy, closes)` returning a 4-band classification:

  band            condition (precedence top-down — 大敗優先)
  ─────────────── ───────────────────────────────────────────
  big_loss        any close vs d1_buy ≤ big_loss_pct  (default -5%)
  big_win         D5 close vs d1_buy  ≥ big_win_day5_pct (default 20%)
  win             D5 close vs d1_buy  ≥ win_pct       (default 5%)
  loss            otherwise

All comparisons use CLOSE prices (matches the post-mortem and Brier
side; replaces the verifier's intraday-high bar which over-counted
"wins" that round-tripped the same session).

`win` was itself a peak-touch rule until the 2026-07 review: "any
close in the window ≥ +5%" scored a spike-and-fade as a win, which is
why the public scoreboard could show a 100% win rate over picks a
reader following them would have lost money on. It now grades on the
D5 close — the realized outcome of the 5-day horizon the topic
promises, and the same basis the scoreboard's `avg_return_pct` uses.
`big_loss` intentionally still fires on any close: it is a
risk-management band, not a return measurement.

Precedence rationale: 大敗 dominates 大勝 because the new rule is
risk-management oriented — a recommendation that drops ≥5% intraday
is a real loss even if the position later rebounds past +20% by D5.
Confirmed by the user in the planning session.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OutcomeBand = Literal["big_win", "win", "big_loss", "loss"]

DEFAULT_BIG_WIN_DAY5_PCT = 20.0
DEFAULT_WIN_PCT = 5.0
DEFAULT_BIG_LOSS_PCT = -5.0


@dataclass(frozen=True)
class OutcomeClassification:
    band: OutcomeBand
    day5_pct: float | None      # D5 close vs d1_buy (None when D5 unresolved)
    peak_pct: float | None      # max(closes) vs d1_buy
    trough_pct: float | None    # min(closes) vs d1_buy


def classify_outcome(
    *,
    d1_buy: float,
    closes: list[float | None],
    big_win_day5_pct: float = DEFAULT_BIG_WIN_DAY5_PCT,
    win_pct: float = DEFAULT_WIN_PCT,
    big_loss_pct: float = DEFAULT_BIG_LOSS_PCT,
    require_d5: bool = True,
) -> OutcomeClassification | None:
    """Classify one symbol's D1-Dn outcome into a band.

    `closes` is the per-day close series. The Nth element (last one
    when length == window) is treated as D5 for the big_win check.
    Missing days (None) are allowed — partial windows still classify
    as big_loss / win / loss, just not big_win (which requires the
    actual D5 close).

    Returns None when no classification is possible (d1_buy invalid
    or every close is missing).
    """
    if d1_buy is None or d1_buy <= 0:
        return None
    if not isinstance(closes, (list, tuple)):
        return None

    numeric_pcts: list[float] = []
    for c in closes:
        if isinstance(c, (int, float)):
            numeric_pcts.append((float(c) - d1_buy) / d1_buy * 100.0)
    if not numeric_pcts:
        return None

    peak_pct = max(numeric_pcts)
    trough_pct = min(numeric_pcts)

    # D5 close is the LAST close in the window, but only valid when
    # the slot actually carries a number — a None tail (partial
    # window) leaves day5_pct unresolved.
    day5_pct: float | None = None
    if closes:
        last = closes[-1]
        if isinstance(last, (int, float)):
            day5_pct = (float(last) - d1_buy) / d1_buy * 100.0

    # `require_d5=False` grades an open window on its most recent
    # resolved close instead of refusing to grade. The post-mortem
    # needs this: it coaches the panel mid-week, when D5 hasn't
    # happened yet, and "no verdict" would leave it with nothing to
    # say. The basis stays a realized close — never the window peak —
    # so the coaching signal and the scoreboard can't disagree about
    # what counts as a win.
    settled_pct = day5_pct
    if settled_pct is None and not require_d5:
        settled_pct = numeric_pcts[-1]

    # Precedence: 大敗 first, then 大勝, then 勝, else 敗.
    #
    # `win` is graded on the D5 CLOSE, not on the window peak. The old
    # peak-touch rule ("any close in D1..D5 ≥ +5%") counted a position
    # that spiked and gave it all back as a win — a number nobody could
    # have realized without a same-week exit the recommendation never
    # specified. D5 close is what a reader following the pick actually
    # gets, and it is the same basis as `avg_return_pct` on the
    # scoreboard, so the two columns can no longer disagree.
    #
    # `big_loss` deliberately keeps the any-close trigger: it is a
    # risk-management band, and a −5% drawdown mid-window is a real
    # loss event even if the price recovers by D5.
    #
    # `peak_pct` / `trough_pct` stay on the result as UI annotations
    # ("期間最高 +8.2% / 最低 −3.1%") — informative, not deciding.
    band: OutcomeBand
    if trough_pct <= big_loss_pct:
        band = "big_loss"
    elif settled_pct is None:
        # Window still open and the caller demands a settled D5.
        # big_loss above can still fire early — a −5% drawdown is
        # already a fact — but win / loss are undecidable, and
        # defaulting them to `loss` would quietly bias every partial
        # window downward. Callers all treat None as "skip this
        # symbol".
        return None
    elif day5_pct is not None and day5_pct >= big_win_day5_pct:
        # 大勝 stays gated on the real D5 close: it is a claim about
        # how the week ended, not about how it was going.
        band = "big_win"
    elif settled_pct >= win_pct:
        band = "win"
    else:
        band = "loss"

    return OutcomeClassification(
        band=band,
        day5_pct=round(day5_pct, 4) if day5_pct is not None else None,
        peak_pct=round(peak_pct, 4),
        trough_pct=round(trough_pct, 4),
    )


@dataclass(frozen=True)
class DiscussionClassification:
    band: OutcomeBand
    winner_symbol: str | None     # symbol that drove the band classification
    classifications: dict[str, OutcomeClassification]


def classify_discussion(
    *,
    per_symbol: dict[str, tuple[float, list[float | None]]],
    big_win_day5_pct: float = DEFAULT_BIG_WIN_DAY5_PCT,
    win_pct: float = DEFAULT_WIN_PCT,
    big_loss_pct: float = DEFAULT_BIG_LOSS_PCT,
) -> DiscussionClassification | None:
    """Roll up per-symbol classifications into a single discussion band.

    Uses the same 大敗優先 precedence at the discussion level: ANY
    symbol triggering big_loss → discussion big_loss; else any big_win
    → big_win; else any win → win; else loss.

    `winner_symbol` is the symbol that drove the chosen band:
      - for big_loss: the symbol with the worst (lowest) trough
      - for big_win: the symbol with the highest D5
      - for win: the symbol with the highest D5
      - for loss: the symbol with the highest D5 (the best of a bad lot)

    Returns None when no symbol has a resolvable classification (every
    d1_buy invalid OR every closes empty).
    """
    if not per_symbol:
        return None
    classifications: dict[str, OutcomeClassification] = {}
    for sym, (d1_buy, closes) in per_symbol.items():
        result = classify_outcome(
            d1_buy=d1_buy, closes=closes,
            big_win_day5_pct=big_win_day5_pct,
            win_pct=win_pct,
            big_loss_pct=big_loss_pct,
        )
        if result is not None:
            classifications[sym] = result
    if not classifications:
        return None

    losers = [
        (sym, c) for sym, c in classifications.items() if c.band == "big_loss"
    ]
    if losers:
        winner = min(losers, key=lambda p: (p[1].trough_pct or 0.0))
        return DiscussionClassification(
            band="big_loss",
            winner_symbol=winner[0],
            classifications=classifications,
        )

    bigs = [
        (sym, c) for sym, c in classifications.items() if c.band == "big_win"
    ]
    if bigs:
        winner = max(bigs, key=lambda p: (p[1].day5_pct or 0.0))
        return DiscussionClassification(
            band="big_win",
            winner_symbol=winner[0],
            classifications=classifications,
        )

    wins = [
        (sym, c) for sym, c in classifications.items() if c.band == "win"
    ]
    if wins:
        winner = max(wins, key=lambda p: (p[1].day5_pct or 0.0))
        return DiscussionClassification(
            band="win",
            winner_symbol=winner[0],
            classifications=classifications,
        )

    # All loss. Pick the best D5 as the "best of a bad lot" so the
    # verdict_reason quotes the same basis the band was decided on.
    winner = max(
        classifications.items(),
        key=lambda p: (p[1].day5_pct or 0.0),
    )
    return DiscussionClassification(
        band="loss",
        winner_symbol=winner[0],
        classifications=classifications,
    )


# ── Backward-compat helpers ─────────────────────────────────────────
#
# Legacy `Discussion.verdict` rows pre-dating this change carry the
# 2-value string `"win"` / `"loss"`. We do NOT rewrite history — the
# new 4-value domain is additive. Every consumer that branches on
# verdict goes through these helpers so legacy rows stay correctly
# categorized as winning / losing without an irreversible migration.


_WINNING_BANDS = frozenset({"win", "big_win"})
_LOSING_BANDS = frozenset({"loss", "big_loss"})


def is_winning_verdict(verdict: str | None) -> bool:
    return verdict in _WINNING_BANDS


def is_losing_verdict(verdict: str | None) -> bool:
    return verdict in _LOSING_BANDS


def band_to_binary(band: str | None) -> int:
    """Collapse a 4-band classification to 0/1 for Brier / calibration.

    The calibrator and Brier loss work over binary outcomes — they
    can't represent "big_loss is worse than loss" without changing
    the math. This collapse keeps the score comparable to historical
    rows (which were already binary).
    """
    return 1 if band in _WINNING_BANDS else 0
