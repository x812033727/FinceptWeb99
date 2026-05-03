"""Empirically validate signal quality against D1-D5 outcomes.

The audit tooling (PR #239-#240) tells us **whether** personas cite
each signal. This script answers the harder question: **does the
signal actually predict moves?**

For every concluded discussion in the lookback window that has
``daily_close_prices`` populated, we extract round-1 signal values
and pair them with the symbol's D5 return. Then aggregate per-signal:

  - Numeric signals → Pearson correlation + sign-match accuracy
  - Categorical signals → mean D5 return per category

Run from the backend dir:

    python -m scripts.signal_quality_check --lookback 60
    python -m scripts.signal_quality_check --lookback 90 --market TW

Read-only. Safe against production.

Interpreting the output:

  pearson_r > 0.3   → meaningfully positive correlation
  pearson_r < -0.3  → meaningfully negative (a contrarian signal)
  |pearson_r| < 0.1 → essentially noise
  sign_match ≈ 50%  → coin flip — signal isn't directional

Categorical buckets:

  bullish: mean D5 = +1.4%  (n=8)   ← favourable bucket
  bearish: mean D5 = -0.8%  (n=12)  ← unfavourable
  neutral: mean D5 = +0.1%  (n=3)   ← null

A signal where the bullish bucket's mean is materially HIGHER than
the bearish bucket's mean is empirically directional. Sign-flip
(bullish < bearish) is the dangerous case — points the wrong way.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from db.session import AsyncSessionLocal
from services.signal_quality_service import (
    SignalQualityReport, compute_signal_quality,
)


def _format_report(report: SignalQualityReport) -> str:
    out: list[str] = []
    out.append("\n=== Signal quality vs D5 returns ===")
    out.append(
        f"Audited: {report.discussions_audited} discussions  "
        f"(lookback {report.lookback_days}d, "
        f"market={report.market or 'all'})"
    )
    if report.discussions_audited == 0:
        out.append(
            "(no concluded discussions with daily_close_prices in the window)"
        )
        return "\n".join(out)

    # Numeric signals
    if any(s.n > 0 for s in report.numeric):
        out.append("\n── Numeric signals (Pearson r, sign-match) ──")
        for s in sorted(report.numeric, key=lambda x: -(x.n or 0)):
            if s.n == 0:
                continue
            r_str = f"{s.pearson_r:+.3f}" if s.pearson_r is not None else "  n/a"
            sm_str = (
                f"{s.sign_match_pct:5.1f}%"
                if s.sign_match_pct is not None else "  n/a"
            )
            out.append(
                f"  n={s.n:4d}  r={r_str}  sign={sm_str}  "
                f"mean_v={s.mean_value:+.3f}  mean_d5={s.mean_d5_return:+.3f}%  "
                f"{s.label}"
            )

    # Categorical signals
    if any(s.n_total > 0 for s in report.categorical):
        out.append("\n── Categorical signals (mean D5 per bucket) ──")
        for s in report.categorical:
            if s.n_total == 0:
                continue
            out.append(f"  {s.label} (n={s.n_total}, path={s.path}):")
            # Sort buckets by mean D5 desc — bullish-direction buckets
            # surface at the top.
            sorted_cats = sorted(
                s.by_category.items(),
                key=lambda kv: -kv[1].mean_d5_return,
            )
            for cat, bucket in sorted_cats:
                out.append(
                    f"      {cat:20s}  n={bucket.n:3d}  "
                    f"mean_d5={bucket.mean_d5_return:+.3f}%  "
                    f"median_d5={bucket.median_d5_return:+.3f}%"
                )

    out.append("")
    out.append("Interpretation hints:")
    out.append("  |r| > 0.3  meaningfully directional")
    out.append("  |r| < 0.1  essentially noise")
    out.append("  bullish-bucket mean >> bearish-bucket mean → signal works")
    return "\n".join(out)


async def _run(lookback: int, market: str | None) -> int:
    async with AsyncSessionLocal() as db:
        report = await compute_signal_quality(
            db, lookback_days=lookback, market=market,
        )
    print(_format_report(report))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--lookback", type=int, default=60,
        help="Days back from now to scan concluded discussions (default 60)",
    )
    ap.add_argument(
        "--market", default=None,
        help="Filter discussions by market: TW / US / GLOBAL (default all)",
    )
    args = ap.parse_args()
    sys.exit(asyncio.run(_run(args.lookback, args.market)))


if __name__ == "__main__":
    main()
