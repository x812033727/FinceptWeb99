/**
 * SweepAggregate headline rows: fold badge + verdict/win-rate KPI
 * grid + D1-D5 P&L strip. All three are small, `agg`-driven summary
 * rows rendered at the top of the card.
 */
import { useTranslation } from "react-i18next";
import { GraduationCap, FlaskConical } from "lucide-react";
import type { SweepAggregate } from "../_helpers";
import { Tile } from "./shared";

export function FoldBadge({ agg }: { agg: SweepAggregate }) {
  const { t } = useTranslation();
  // Fold metadata only lands on scope="sweep" responses. Strategy
  // aggregates roll up across all fold kinds and don't expose it.
  const fold = agg.fold_kind;
  if (!fold || fold === "production") return null;
  const styles = fold === "train"
    ? "bg-blue-900/30 text-blue-200 border-blue-700/50"
    : "bg-purple-900/30 text-purple-200 border-purple-700/50";
  const label = fold === "train"
    ? t("aggregate.fold_train", "Train fold (in-sample)")
    : t("aggregate.fold_test", "Test fold (OOS)");
  const FoldIcon = fold === "train" ? GraduationCap : FlaskConical;
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className={`inline-flex items-center gap-1 border rounded px-1.5 py-0.5 ${styles}`}>
        <FoldIcon className="h-3 w-3" aria-hidden="true" />
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

export function KpiRow({ agg }: { agg: SweepAggregate }) {
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

export function PnlRow({ agg }: { agg: SweepAggregate }) {
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
