"""Read-time confidence tier for daily picks (spec: confidence-tiering).

Pure and derived — no storage, no behaviour change, retroactively
consistent for every historical pick. Missing/malformed inputs degrade
to "watch" (or None when there is no pick at all): the recommend tier
must be EARNED by clean signals, never granted by absent ones.

Thresholds are calibrated against graded experiment picks
(scripts/calibrate_pick_tier.py); treat the constants as data, not
opinion — recalibrations ship with the sweep output attached.

Calibration 2026-07-28 (23 graded picks, D5 excess vs _TAIEX_TR):
`T_CONSENSUS=0.75, T_HALLUC=60` is the ONLY cell meeting the
pre-registered rule (tier win rate >= 0.8 with >= 1/3 coverage:
recommend 15 picks / 80% win vs watch 75%). The provisional 0.85/2
pair was mis-scaled: `hallucination_warnings` counts every data-
citation warning across all rounds (observed range 3-54 in the
five-round era), so <=2 put EVERY pick in watch. Worse, the raw
count anti-selects at every realistic cutoff (warning-light picks
won LESS often), so T_HALLUC=60 deliberately renders that gate
inert until a per-pick attribution exists (the spec's "warnings
referencing signals the pick relies on"). The per-tier scoreboard
columns publish each tier's live win rate, so a mis-calibrated
split indicts itself.

2026-08 note: `_AUTO_ROUNDS` dropped 5 → 3, shrinking the count
scale (3-round live sessions observe 0-17, p90=9). That leaves 60
still unreachable — the gate stays inert BY DESIGN, not by drift.
Do not "recalibrate" it downward without first building the
per-pick attribution the 07-28 sweep demanded; the raw count's
anti-selection finding is independent of the round count.
"""
from __future__ import annotations

from typing import Any

T_CONSENSUS = 0.75
T_HALLUC = 60


def tier_for(conclusion: dict[str, Any] | None) -> str | None:
    if not isinstance(conclusion, dict):
        return None
    symbols = conclusion.get("recommended_symbols") or []
    if not symbols:
        return None

    consensus = conclusion.get("consensus_score")
    if not isinstance(consensus, (int, float)) or consensus < T_CONSENSUS:
        return "watch"

    quality = conclusion.get("quality_signals")
    warnings = (
        quality.get("hallucination_warnings")
        if isinstance(quality, dict) else None
    )
    n_warnings = len(warnings) if isinstance(warnings, list) else T_HALLUC + 1
    if n_warnings > T_HALLUC:
        return "watch"
    return "recommend"
