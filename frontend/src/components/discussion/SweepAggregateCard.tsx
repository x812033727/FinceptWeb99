import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import {
  fetchSweepAggregate,
  fetchStrategyAggregate,
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
      <p className="text-[11px] text-red-300 px-1 py-0.5">
        {(error as Error).message}
      </p>
    );
  }
  if (!data) return null;

  return (
    <div className="space-y-2 text-xs">
      <KpiRow agg={data} />
      <PnlRow agg={data} />
      <PersonaTable agg={data} />
      <LessonsList agg={data} />
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
        value={`${v.win} / ${v.loss}`}
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
    ? "text-emerald-300"
    : accent === "red"
    ? "text-red-300"
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
            p === null ? "" : p > 0 ? "text-emerald-300" : "text-red-300";
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
              <td className="py-0.5 text-right text-emerald-300">
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
            <span className="inline-block bg-amber-900/30 text-amber-300 border border-amber-800/50 rounded px-1 py-0.5 mr-1.5 text-[9px] uppercase">
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
