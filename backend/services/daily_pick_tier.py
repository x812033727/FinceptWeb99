"""Read-time confidence tier for daily picks (spec: confidence-tiering).

Pure and derived — no storage, no behaviour change, retroactively
consistent for every historical pick. Missing/malformed inputs degrade
to "watch" (or None when there is no pick at all): the recommend tier
must be EARNED by clean signals, never granted by absent ones.

Thresholds are calibrated against graded experiment picks
(scripts/calibrate_pick_tier.py); treat the constants as data, not
opinion — recalibrations ship with the sweep output attached.
"""
from __future__ import annotations

from typing import Any

T_CONSENSUS = 0.85
T_HALLUC = 2


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
