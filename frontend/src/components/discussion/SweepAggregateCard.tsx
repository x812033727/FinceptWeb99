import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import {
  fetchSweepAggregate,
  fetchStrategyAggregate,
  type ReliabilityBucket,
  type SweepAggregate,
} from "./_helpers";

/** PR-B sweep / strategy aggregate KPIs.
 *
 * Renders the verdict tile, win-rate, D1-D5 P&L bar, per-persona
 * table, and recent lessons feed for either:
 *   - one sweep (via `sweepId`)
 *   - one strategy template across every sweep (via `strategyId`)
 *
 * Re-fetches every 30s while a sweep is still resolving (the
 * verdict + scoreboard cron updates child discussions on its own
 * cadence, so we keep refreshing until everything settles). */

export function SweepAggregateCard({
  sweepId,
  strategyId,
  /** When `enabled` is false the query stays cold — the embedded
   *  use case (inside a sweep row's expand) sets this only when the
   *  row is opened. */
  enabled = true,
  refreshMs = 30000,
}: {
  sweepId?: string;
  strategyId?: string;
  enabled?: boolean;
  refreshMs?: number;
}) {
  const { t } = useTranslation();
  const queryKey = sweepId
    ? ["sweep-aggregate", sweepId]
    : ["strategy-aggregate", strategyId];
  const queryFn = sweepId
    ? () => fetchSweepAggregate(sweepId)
    : () => fetchStrategyAggregate(strategyId!);
  const { data, isLoading, error } = useQuery({
    queryKey,
    queryFn,
    enabled: enabled && Boolean(sweepId || strategyId),
    refetchInterval: (q) => {
      const d = q.state.data as SweepAggregate | undefined;
      if (!d) return false;
      return d.verdict_counts.pending > 0 ? refreshMs : false;
    },
  });

  if (!enabled) return null;
  if (isLoading) {
    return (
      <p className="text-[11px] text-muted-foreground animate-pulse px-1 py-0.5">
        {t("common.loading")}
      </p>
    );
  }
  if (error) {
    return (
      <p className="text-[11px] text-danger px-1 py-0.5">
        {(error as Error).message}
      </p>
    );
  }
  if (!data) return null;

  return (
    <div className="space-y-2 text-xs">
      <FoldBadge agg={data} />
      <KpiRow agg={data} />
      <BrierRow agg={data} />
      <WalkForwardCompareSection testAgg={data} />
      <PnlRow agg={data} />
      <PersonaTable agg={data} />
      <LessonsList agg={data} />
    </div>
  );
}

function FoldBadge({ agg }: { agg: SweepAggregate }) {
  const { t } = useTranslation();
  // Fold metadata only lands on scope="sweep" responses. Strategy
  // aggregates roll up across all fold kinds and don't expose it.
  const fold = agg.fold_kind;
  if (!fold || fold === "production") return null;
  const styles = fold === "train"
    ? "bg-blue-900/30 text-blue-200 border-blue-700/50"
    : "bg-purple-900/30 text-purple-200 border-purple-700/50";
  const label = fold === "train"
    ? t("aggregate.fold_train", "🎓 Train fold (in-sample)")
    : t("aggregate.fold_test", "🔬 Test fold (OOS)");
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className={`border rounded px-1.5 py-0.5 ${styles}`}>
        {label}
      </span>
      {fold === "test" && agg.parent_sweep_id ? (
        <span
          className="text-muted-foreground"
          title={t(
            "aggregate.fold_pair_tip",
            "這是配對 walk-forward 的 test 折,點 sweep 列表可找到對應 train 折",
          )}
        >
          {t("aggregate.fold_paired_with", "配對 train sweep")}:{" "}
          <code className="font-mono">
            {agg.parent_sweep_id.slice(0, 8)}
          </code>
        </span>
      ) : null}
    </div>
  );
}

function fmtPct(n: number | null, decimals = 1): string {
  if (n === null) return "—";
  const pct = n * 100;
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(decimals)}%`;
}

function KpiRow({ agg }: { agg: SweepAggregate }) {
  const { t } = useTranslation();
  const v = agg.verdict_counts;
  // 4-band rollup: big_win + win on the upside, big_loss + loss on
  // the downside. Hover tooltip carries the 4-way breakdown so the
  // dashboard stays compact while still surfacing the detail.
  const bigWin = v.big_win ?? 0;
  const bigLoss = v.big_loss ?? 0;
  const totalWin = bigWin + v.win;
  const totalLoss = bigLoss + v.loss;
  const winLossTip = t(
    "aggregate.win_loss_tip",
    "大勝 {{bw}} / 勝 {{w}} | 大敗 {{bl}} / 敗 {{l}}",
    { bw: bigWin, w: v.win, bl: bigLoss, l: v.loss },
  );
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
      <Tile
        label={t("aggregate.discussions", "討論數")}
        value={String(agg.discussions_total)}
      />
      <Tile
        label={t("aggregate.win_rate", "勝率")}
        value={agg.win_rate === null ? "—" : `${(agg.win_rate * 100).toFixed(0)}%`}
        accent="emerald"
      />
      <Tile
        label={t("aggregate.win_loss", "勝/負")}
        value={`${totalWin} / ${totalLoss}`}
        labelTip={winLossTip}
      />
      <Tile
        label={t("aggregate.pending", "未驗")}
        value={`${v.pending} / ${v.unverifiable}`}
        labelTip={t("aggregate.pending_tip", "未驗證 / 無法驗證")}
      />
    </div>
  );
}

function Tile({
  label, value, accent, labelTip,
}: {
  label: string;
  value: string;
  accent?: "emerald" | "red";
  labelTip?: string;
}) {
  const cls = accent === "emerald"
    ? "text-success"
    : accent === "red"
    ? "text-danger"
    : "text-foreground";
  return (
    <div className="bg-secondary/30 border border-border rounded p-1.5">
      <p
        className="text-[10px] text-muted-foreground uppercase tracking-wider"
        title={labelTip}
      >
        {label}
      </p>
      <p className={`text-sm font-semibold font-mono ${cls}`}>{value}</p>
    </div>
  );
}

function BrierRow({ agg }: { agg: SweepAggregate }) {
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
        className="text-[10px] text-muted-foreground uppercase tracking-wider"
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
        <p className={`text-[10px] ${deltaClass}`}>
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
      <p className="text-[10px] text-muted-foreground">
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
            else color = "bg-blue-500/70";
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

/**
 * Side-by-side train vs test fold KPI comparison (PR-A1 + PR-C1
 * follow-up). Lives inside SweepAggregateCard but only renders
 * when the current sweep is a `test` fold with a paired
 * `parent_sweep_id`. Click to expand → fetches the train sibling
 * via `fetchSweepAggregate` and renders the two side by side so
 * the operator can see how much of the train fold's apparent
 * skill survived the OOS check.
 *
 * The interesting cells:
 *   • Brier raw / calibrated — train < test typically (in-sample
 *     fits noise the test fold can't reproduce). Big gap = strong
 *     overfitting evidence.
 *   • Win rate — same direction. A train fold winning 80% but the
 *     test sibling winning 40% means the persona weights learned
 *     from train are picking out coincidences, not signal.
 *   • Avg PnL D5 — same.
 */
function WalkForwardCompareSection({ testAgg }: { testAgg: SweepAggregate }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  if (
    testAgg.fold_kind !== "test"
    || !testAgg.parent_sweep_id
  ) {
    return null;
  }
  return (
    <div className="bg-secondary/20 border border-border rounded p-2 space-y-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-[11px] text-purple-300 hover:text-purple-200"
      >
        {open
          ? t("aggregate.compare_hide", "▼ 收起 train 折比較")
          : t(
              "aggregate.compare_show",
              "🔍 展開 train vs test 並排 KPI(in-sample vs OOS)",
            )}
      </button>
      {open ? (
        <CompareGrid parentSweepId={testAgg.parent_sweep_id} testAgg={testAgg} />
      ) : null}
    </div>
  );
}

function CompareGrid({
  parentSweepId, testAgg,
}: { parentSweepId: string; testAgg: SweepAggregate }) {
  const { t } = useTranslation();
  const { data: trainAgg, isLoading, error } = useQuery({
    queryKey: ["sweep-aggregate", parentSweepId],
    queryFn: () => fetchSweepAggregate(parentSweepId),
  });
  if (isLoading) {
    return (
      <p className="text-[11px] text-muted-foreground animate-pulse">
        {t("common.loading")}
      </p>
    );
  }
  if (error || !trainAgg) {
    return (
      <p className="text-[11px] text-danger">
        {(error as Error | undefined)?.message
          ?? t(
              "aggregate.compare_train_missing",
              "找不到對應的 train 折(可能已被刪除)",
            )}
      </p>
    );
  }
  return (
    <div className="space-y-1">
      <div className="grid grid-cols-3 gap-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        <div>{t("aggregate.compare_metric", "指標")}</div>
        <div className="text-center">
          {t("aggregate.compare_train", "🎓 Train (in-sample)")}
        </div>
        <div className="text-center">
          {t("aggregate.compare_test", "🔬 Test (OOS)")}
        </div>
      </div>
      <CompareRow
        label={t("aggregate.brier_raw", "Raw Brier")}
        train={trainAgg.brier_score ?? null}
        test={testAgg.brier_score ?? null}
        // Lower is better for Brier — flip the comparison logic.
        lowerIsBetter
      />
      <CompareRow
        label={t("aggregate.brier_calibrated", "Calibrated Brier")}
        train={trainAgg.calibrated_brier_score ?? null}
        test={testAgg.calibrated_brier_score ?? null}
        lowerIsBetter
      />
      <CompareRow
        label={t("aggregate.win_rate", "勝率")}
        train={trainAgg.win_rate}
        test={testAgg.win_rate}
        format="percent"
      />
      <CompareRow
        label={t("aggregate.avg_d5_pnl", "D5 平均報酬")}
        train={trainAgg.avg_pnl_pct[4] ?? null}
        test={testAgg.avg_pnl_pct[4] ?? null}
        format="signed_percent"
      />
      <p className="text-[10px] text-muted-foreground mt-1">
        {t(
          "aggregate.compare_overfitting_tip",
          "Train < Test 差距愈大 = 模型在 train 折擬合了雜訊。" +
          "如果 Test 比 Train 差很多,該 strategy 的 persona 權重學習可能過擬合,考慮改 train_window 大小或縮減 fold 數。",
        )}
      </p>
    </div>
  );
}

function CompareRow({
  label, train, test, lowerIsBetter, format,
}: {
  label: string;
  train: number | null;
  test: number | null;
  lowerIsBetter?: boolean;
  format?: "percent" | "signed_percent";
}) {
  const fmt = (n: number | null): string => {
    if (n === null || n === undefined) return "—";
    if (format === "percent") return `${(n * 100).toFixed(0)}%`;
    if (format === "signed_percent") {
      const pct = n * 100;
      const sign = pct > 0 ? "+" : "";
      return `${sign}${pct.toFixed(2)}%`;
    }
    return n.toFixed(3);
  };
  // Highlight the test column when its value is meaningfully
  // worse than train. "Worse" depends on direction — lower
  // Brier is better, higher win_rate / pnl is better.
  let testCls = "text-foreground";
  if (train !== null && test !== null) {
    const epsilon = 0.005;   // 0.5pp / 0.005 brier delta
    const testWorse = lowerIsBetter
      ? test > train + epsilon
      : test < train - epsilon;
    const testBetter = lowerIsBetter
      ? test < train - epsilon
      : test > train + epsilon;
    if (testWorse) testCls = "text-danger";
    else if (testBetter) testCls = "text-success";
  }
  return (
    <div className="grid grid-cols-3 gap-1 text-[11px] font-mono">
      <div className="text-muted-foreground">{label}</div>
      <div className="text-center">{fmt(train)}</div>
      <div className={`text-center ${testCls}`}>{fmt(test)}</div>
    </div>
  );
}


function PnlRow({ agg }: { agg: SweepAggregate }) {
  const { t } = useTranslation();
  return (
    <div className="bg-secondary/20 border border-border rounded p-2">
      <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">
        {t("aggregate.avg_pnl", "平均報酬 D1-D5（推薦標的）")}
      </p>
      <div className="grid grid-cols-5 gap-1">
        {agg.avg_pnl_pct.map((p, i) => {
          const accent =
            p === null ? "" : p > 0 ? "text-up" : "text-down";
          return (
            <div key={i} className="text-center">
              <p className="text-[10px] text-muted-foreground">D{i + 1}</p>
              <p className={`text-xs font-mono font-semibold ${accent}`}>
                {fmtPct(p)}
              </p>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function PersonaTable({ agg }: { agg: SweepAggregate }) {
  const { t } = useTranslation();
  if (agg.per_persona.length === 0) return null;
  const sorted = [...agg.per_persona].sort((a, b) => {
    const ar = a.hit_rate ?? -1;
    const br = b.hit_rate ?? -1;
    return br - ar;
  });
  return (
    <div className="bg-secondary/20 border border-border rounded p-2">
      <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">
        {t("aggregate.persona_stats", "專家命中表")}
      </p>
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="font-normal">{t("aggregate.persona", "專家")}</th>
            <th className="font-normal text-right">
              {t("aggregate.hit", "命中")}
            </th>
            <th className="font-normal text-right">
              {t("aggregate.win_count", "勝")}
            </th>
            <th className="font-normal text-right">
              {t("aggregate.disc_count", "場次")}
            </th>
            <th
              className="font-normal text-right"
              title={t("aggregate.agree_dissent_tip",
                       "agree+supplement / dissent")}
            >
              A/D
            </th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {sorted.map((p) => (
            <tr key={p.persona_id} className="border-t border-border/40">
              <td className="py-0.5 truncate max-w-[8em]">{p.persona_id}</td>
              <td className="py-0.5 text-right">
                {p.hit_rate === null ? "—" : `${(p.hit_rate * 100).toFixed(0)}%`}
              </td>
              <td className="py-0.5 text-right text-success">
                {p.win_count}
              </td>
              <td className="py-0.5 text-right">{p.discussions_count}</td>
              <td className="py-0.5 text-right text-muted-foreground">
                {p.agree_turn_count}/{p.dissent_turn_count}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LessonsList({ agg }: { agg: SweepAggregate }) {
  const { t } = useTranslation();
  if (agg.lessons.length === 0) return null;
  return (
    <details className="bg-secondary/20 border border-border rounded p-2">
      <summary className="text-[10px] text-muted-foreground uppercase tracking-wider cursor-pointer">
        {t("aggregate.lessons", "事後檢討教訓")}（{agg.lessons.length}）
      </summary>
      <ul className="mt-1.5 space-y-1">
        {agg.lessons.map((l, i) => (
          <li key={i} className="text-[11px]">
            <span className="inline-block bg-warning/10 text-warning border border-warning/30 rounded px-1 py-0.5 mr-1.5 text-[9px] uppercase">
              {l.category}
            </span>
            <span className="text-muted-foreground mr-1">{l.as_of_date}</span>
            <span>{l.lesson_text}</span>
            {l.related_symbols.length > 0 && (
              <span className="ml-1 text-muted-foreground font-mono">
                [{l.related_symbols.join(", ")}]
              </span>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}
