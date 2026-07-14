/**
 * SweepAggregate calibration section: the Brier raw/calibrated/Δ
 * grid plus the 10-bucket reliability diagram. Renders nothing when
 * the sweep has no scoring data (pre-scoring or all-pending sweeps).
 */
import { useTranslation } from "react-i18next";
import type { ReliabilityBucket, SweepAggregate } from "../_helpers";
import { Tile } from "./shared";

export function BrierRow({ agg }: { agg: SweepAggregate }) {
  const { t } = useTranslation();
  const raw = agg.brier_score;
  const calibrated = agg.calibrated_brier_score;
  const reliability = agg.reliability;
  // Skip the whole section when the sweep has no scoring data —
  // pre-PR-C1 sweeps + all-pending sweeps shouldn't render an
  // empty placeholder.
  if (
    raw === null || raw === undefined
  ) {
    return null;
  }
  const delta =
    raw !== null && calibrated !== null && calibrated !== undefined
      ? calibrated - raw
      : null;
  const deltaClass =
    delta === null
      ? ""
      : delta < -0.001
      ? "text-success"
      : delta > 0.001
      ? "text-danger"
      : "text-muted-foreground";
  const deltaSign = delta === null ? "" : delta > 0 ? "+" : "";
  return (
    <div className="bg-secondary/20 border border-border rounded p-2 space-y-1.5">
      <p
        className="text-micro text-muted-foreground uppercase tracking-wider"
        title={t(
          "aggregate.brier_tip",
          "Brier 分數 = 預測機率與實際結果的均方誤差。" +
          "0 完美,0.25 是 0.5 機率亂猜的基線,愈低愈好。",
        )}
      >
        {t("aggregate.brier_label", "校準度量 (Brier ↓)")}
      </p>
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-1.5">
        <Tile
          label={t("aggregate.brier_raw", "Raw Brier")}
          value={raw.toFixed(3)}
          labelTip={t(
            "aggregate.brier_raw_tip",
            "用 synthesizer 原始 confidence 算出的 Brier。" +
            "樣本數 = {{n}}",
            { n: agg.brier_samples ?? 0 },
          )}
        />
        <Tile
          label={t("aggregate.brier_calibrated", "Calibrated Brier")}
          value={
            calibrated === null || calibrated === undefined
              ? "n/a"
              : calibrated.toFixed(3)
          }
          accent={
            delta !== null && delta < -0.001
              ? "emerald"
              : delta !== null && delta > 0.001
              ? "red"
              : undefined
          }
          labelTip={t(
            "aggregate.brier_calibrated_tip",
            "套上 isotonic 曲線後的 Brier。比 Raw 低 = 校準有用;" +
            "n/a = 該 sweep 沒有 calibrated 資料",
          )}
        />
        {delta !== null ? (
          <Tile
            label={t("aggregate.brier_delta", "改善 (Δ)")}
            value={`${deltaSign}${delta.toFixed(3)}`}
            // accent is informational, can't reuse the standard
            // Tile accent because we want a custom shade per sign.
          />
        ) : null}
      </div>
      {delta !== null ? (
        <p className={`text-micro ${deltaClass}`}>
          {delta < -0.001
            ? t(
                "aggregate.brier_improving",
                "✔ 校準曲線降低 Brier,即時預測比 raw 更貼近真實",
              )
            : delta > 0.001
            ? t(
                "aggregate.brier_worse",
                "⚠ 校準後反而變差,曲線可能套到不同 regime,考慮重 fit",
              )
            : t(
                "aggregate.brier_neutral",
                "校準前後 Brier 幾乎相同 — 曲線目前無顯著效果",
              )}
        </p>
      ) : null}
      {reliability && reliability.length > 0 ? (
        <ReliabilityChart buckets={reliability} />
      ) : null}
    </div>
  );
}

function ReliabilityChart({ buckets }: { buckets: ReliabilityBucket[] }) {
  const { t } = useTranslation();
  // Render 10 columns side-by-side. Each column has two stacked
  // visual cues:
  //   • a 16px-tall track, with `hit_rate` shown as a filled
  //     vertical bar from the bottom, color-coded vs the bucket
  //     midpoint (perfect calibration = bar fills to mid-line)
  //   • a thin horizontal mark at the bucket midpoint as the
  //     ideal-calibration reference line
  // Empty buckets render with a striped placeholder.
  const maxCount = Math.max(1, ...buckets.map((b) => b.count));
  return (
    <div className="space-y-0.5">
      <p className="text-micro text-muted-foreground">
        {t(
          "aggregate.reliability_label",
          "Reliability — 按 confidence 分 10 桶,實際命中率 vs 預測值",
        )}
      </p>
      <div className="flex items-end gap-0.5 h-12 bg-secondary/40 border border-border rounded p-1">
        {buckets.map((b, i) => {
          const mid = (b.bucket_lower + b.bucket_upper) / 2;
          const hit = b.hit_rate;
          const empty = hit === null || b.count === 0;
          // Bar height as % of track. Empty buckets render at 0%
          // with a dashed border so the diagram stays continuous.
          const heightPct = empty ? 0 : Math.round((hit ?? 0) * 100);
          // Color: under-confident (hit > mid) green; over-confident
          // (hit < mid) red; neutral grey.
          let color = "bg-muted-foreground/30";
          if (!empty && hit !== null) {
            if (hit > mid + 0.05) color = "bg-success/70";
            else if (hit < mid - 0.05) color = "bg-danger/70";
            else color = "bg-info/70";
          }
          // Reference line: bucket midpoint as percentage.
          const refLinePct = Math.round(mid * 100);
          // Width — count-weighted, so a heavily-populated bucket
          // dominates visual real estate.
          const widthPct = Math.max(4, (b.count / maxCount) * 100);
          return (
            <div
              key={i}
              className="relative h-full flex-1"
              style={{ minWidth: `${widthPct / buckets.length}%` }}
              title={t(
                "aggregate.reliability_bucket_tip",
                "桶 {{lo}}-{{hi}}: 樣本 {{n}},mean conf {{mc}},hit rate {{hr}}",
                {
                  lo: b.bucket_lower.toFixed(1),
                  hi: b.bucket_upper.toFixed(1),
                  n: b.count,
                  mc:
                    b.mean_confidence === null
                      ? "—"
                      : b.mean_confidence.toFixed(2),
                  hr: empty ? "—" : `${heightPct}%`,
                },
              )}
            >
              {/* reference line at bucket midpoint */}
              <div
                className="absolute left-0 right-0 border-t border-dashed border-foreground/30"
                style={{ bottom: `${refLinePct}%` }}
              />
              {/* hit-rate bar */}
              <div
                className={`absolute bottom-0 left-0 right-0 ${color} ${empty ? "border border-dashed border-muted-foreground/40" : ""}`}
                style={{ height: `${heightPct}%` }}
              />
            </div>
          );
        })}
      </div>
      <p className="text-[9px] text-muted-foreground">
        {t(
          "aggregate.reliability_legend",
          "🟢 underconfident  🔵 well calibrated  🔴 overconfident  ┄ ideal line",
        )}
      </p>
    </div>
  );
}
