"""Lossless context minifier for discussion LLM prompts.

`_minify_for_prompt` shrinks the serialized market-context JSON sent to
each persona (and the synthesizer) WITHOUT changing what the model sees
semantically:

  - drops dict keys whose value is ``None`` — connector "no data" fields
    that ``json.dumps`` would otherwise emit as ``"field":null`` noise,
  - collapses float noise to 6 significant figures (``650.0000001`` ->
    ``650.0``) while preserving small magnitudes (``0.000123450`` stays
    ``0.00012345`` — significant *figures*, not decimal places, so small
    values never round to zero),
  - converts ``NaN`` / ``Inf`` to a dropped key (also guarantees the
    output is valid JSON — ``json.dumps`` would otherwise emit bare
    ``NaN`` / ``Infinity`` which no strict JSON parser accepts).

``False`` / ``0`` / ``""`` / empty collections are KEPT — they carry
meaning (``is_intraday: false``, ``change_pct: 0``). The personas cite
the same numbers either way, so this is a pure size reduction with zero
semantic change.

Pure (no DB / LLM / I/O). Applied right before ``json.dumps`` at the two
prompt-assembly sites (``round_runner`` per-persona, ``synthesizer``).
"""
from __future__ import annotations

import math
from typing import Any

_SIG_FIGS = 6


def _round_float(x: float) -> float | None:
    """Collapse float noise to `_SIG_FIGS` significant figures. Returns
    ``None`` for NaN / Inf so the caller drops the key (and the output
    stays valid JSON)."""
    if math.isnan(x) or math.isinf(x):
        return None
    if x == 0:
        return 0.0
    return float(f"{x:.{_SIG_FIGS}g}")


def _minify_for_prompt(obj: Any) -> Any:
    """Recursively drop ``None`` dict values + collapse float noise.

    Lossless for the LLM: no datum a persona would cite is removed, only
    null placeholders and trailing float noise. List elements keep their
    positions (a NaN element becomes ``null`` rather than being dropped,
    so row arrays don't go ragged in length).
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return _round_float(obj)
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            mv = _minify_for_prompt(v)
            if mv is None:
                continue
            out[k] = mv
        return out
    if isinstance(obj, (list, tuple)):
        return [_minify_for_prompt(v) for v in obj]
    return obj
