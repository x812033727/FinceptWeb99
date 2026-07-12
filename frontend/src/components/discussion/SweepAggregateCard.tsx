import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import {
  fetchSweepAggregate,
  fetchStrategyAggregate,
  type SweepAggregate,
} from "./_helpers";
import { FoldBadge, KpiRow, PnlRow } from "./SweepAggregate/kpis";
import { BrierRow } from "./SweepAggregate/brier";
import { WalkForwardCompareSection } from "./SweepAggregate/walkforward";
import { PersonaTable, LessonsList } from "./SweepAggregate/breakdown";

/** PR-B sweep / strategy aggregate KPIs.
 *
 * Renders the verdict tile, win-rate, D1-D5 P&L bar, per-persona
 * table, and recent lessons feed for either:
 *   - one sweep (via `sweepId`)
 *   - one strategy template across every sweep (via `strategyId`)
 *
 * Re-fetches every 30s while a sweep is still resolving (the
 * verdict + scoreboard cron updates child discussions on its own
 * cadence, so we keep refreshing until everything settles).
 *
 * The presentational sections live split by concern under
 * `./SweepAggregate/*` (kpis / brier / walkforward / breakdown,
 * with the shared `Tile` primitive in `shared`); this file owns the
 * data-fetch + section composition. */

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
