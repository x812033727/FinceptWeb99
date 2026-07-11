/**
 * PR-5b: per-persona leaderboard.
 *
 * Sortable table — one row per persona that's participated in the
 * window, ranked by win-attribution rate then participation count.
 * Sits on a strategy detail (when scoped by `strategyId` it shows
 * weight trend + average weight) or as an admin-side global view
 * (without strategy_id, weight columns hide; status counts span
 * all the operator's strategies).
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { useCollapsible } from "@/hooks/useCollapsible";
import type { PersonaLeaderboardResponse, PersonaLeaderboardRow } from "./_helpers";


type SortKey = "win_rate" | "participation" | "weight" | "trend";


function trendIndicator(delta: number | null): { glyph: string; tone: string } {
  if (delta === null) return { glyph: "→", tone: "text-muted-foreground" };
  if (delta > 0.02) return { glyph: "↑", tone: "text-success" };
  if (delta < -0.02) return { glyph: "↓", tone: "text-danger" };
  return { glyph: "→", tone: "text-muted-foreground" };
}


export function PersonaLeaderboardCard({
  strategyId,
  windowDays = 90,
  personaName,
}: {
  strategyId?: string;
  windowDays?: number;
  personaName?: (id: string) => string;
}) {
  const { t } = useTranslation();
  const storageKey = strategyId
    ? `strategy.${strategyId}.persona-leaderboard`
    : "global.persona-leaderboard";
  const { open, toggle } = useCollapsible(storageKey, false);
  const [sortKey, setSortKey] = useState<SortKey>("win_rate");

  const { data, isLoading } = useQuery({
    queryKey: ["persona-leaderboard", strategyId ?? "global", windowDays],
    enabled: open,
    queryFn: async () => {
      const params = new URLSearchParams();
      params.set("days", String(windowDays));
      if (strategyId) params.set("strategy_id", strategyId);
      const { data } = await api.get<PersonaLeaderboardResponse>(
        `/discussion/personas/leaderboard?${params.toString()}`,
      );
      return data;
    },
    staleTime: 60_000,
  });

  const items = useMemo(() => {
    const rows = (data?.items ?? []).slice();
    rows.sort((a, b) => {
      const cmp = (x: number | null, y: number | null) => {
        if (x === null && y === null) return 0;
        if (x === null) return 1;
        if (y === null) return -1;
        return y - x;
      };
      switch (sortKey) {
        case "win_rate":
          return cmp(a.win_attribution_rate, b.win_attribution_rate)
            || (b.participation_count - a.participation_count);
        case "participation":
          return b.participation_count - a.participation_count;
        case "weight":
          return cmp(a.average_weight, b.average_weight);
        case "trend":
          return cmp(a.weight_trend_30d, b.weight_trend_30d);
      }
    });
    return rows;
  }, [data, sortKey]);

  const showWeightCols = strategyId !== undefined;

  return (
    <div className="border-t border-border/50 mt-2 pt-2">
      <button
        type="button"
        onClick={toggle}
        className="text-[11px] text-muted-foreground hover:text-foreground
                   flex items-center gap-1"
      >
        <span className="w-3 inline-block">{open ? "▼" : "▶"}</span>
        {t("discussion.leaderboard.title", { days: windowDays })}
      </button>
      {open && (
        <div className="mt-2">
          {isLoading && (
            <div className="text-[11px] text-muted-foreground animate-pulse">
              {t("common.loading")}
            </div>
          )}
          {!isLoading && items.length === 0 && (
            <div className="text-[11px] text-muted-foreground italic">
              {t("discussion.leaderboard.empty")}
            </div>
          )}
          {items.length > 0 && (
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-muted-foreground border-b border-border">
                  <th className="text-left py-1">
                    {t("discussion.leaderboard.col.persona")}
                  </th>
                  <SortHeader
                    label={t("discussion.leaderboard.col.win_rate")}
                    active={sortKey === "win_rate"}
                    onClick={() => setSortKey("win_rate")}
                    align="right"
                  />
                  <SortHeader
                    label={t("discussion.leaderboard.col.participation")}
                    active={sortKey === "participation"}
                    onClick={() => setSortKey("participation")}
                    align="right"
                  />
                  {showWeightCols && (
                    <>
                      <SortHeader
                        label={t("discussion.leaderboard.col.weight")}
                        active={sortKey === "weight"}
                        onClick={() => setSortKey("weight")}
                        align="right"
                      />
                      <SortHeader
                        label={t("discussion.leaderboard.col.trend")}
                        active={sortKey === "trend"}
                        onClick={() => setSortKey("trend")}
                        align="right"
                      />
                    </>
                  )}
                  <th className="text-right py-1">
                    {t("discussion.leaderboard.col.status")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((r) => (
                  <Row
                    key={r.persona_id}
                    row={r}
                    showWeightCols={showWeightCols}
                    personaName={personaName}
                  />
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}


function SortHeader({
  label, active, onClick, align,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  align: "left" | "right";
}) {
  return (
    <th className={`py-1 ${align === "right" ? "text-right" : "text-left"}`}>
      <button
        type="button"
        onClick={onClick}
        className={`hover:text-foreground ${active ? "text-foreground" : ""}`}
      >
        {label} {active && "↓"}
      </button>
    </th>
  );
}


function Row({
  row, showWeightCols, personaName,
}: {
  row: PersonaLeaderboardRow;
  showWeightCols: boolean;
  personaName?: (id: string) => string;
}) {
  const trend = trendIndicator(row.weight_trend_30d);
  const display = personaName ? personaName(row.persona_id) : row.persona_id;
  const winRatePct =
    row.win_attribution_rate !== null
      ? `${(row.win_attribution_rate * 100).toFixed(0)}%`
      : "—";
  const winRateColor =
    row.win_attribution_rate === null
      ? "text-muted-foreground"
      : row.win_attribution_rate >= 0.6
        ? "text-success"
        : row.win_attribution_rate >= 0.4
          ? "text-warning"
          : "text-danger";
  return (
    <tr className="border-b border-border/40">
      <td className="py-1">
        <span className="font-mono">{display}</span>
      </td>
      <td className={`py-1 text-right tabular-nums ${winRateColor}`}>
        {winRatePct}
        <span className="text-muted-foreground text-[10px] ml-1">
          ({row.win_attribution_count}/{row.participation_count})
        </span>
      </td>
      <td className="py-1 text-right tabular-nums">
        {row.participation_count}
      </td>
      {showWeightCols && (
        <>
          <td className="py-1 text-right tabular-nums">
            {row.average_weight !== null
              ? row.average_weight.toFixed(2)
              : "—"}
          </td>
          <td className={`py-1 text-right tabular-nums ${trend.tone}`}>
            <span className="inline-block w-3">{trend.glyph}</span>
            {row.weight_trend_30d !== null
              ? `${row.weight_trend_30d >= 0 ? "+" : ""}${row.weight_trend_30d.toFixed(2)}`
              : ""}
          </td>
        </>
      )}
      <td className="py-1 text-right">
        {row.frozen_in_strategies > 0 && (
          <span
            className="inline-block text-[10px] mr-1 text-zinc-400"
            title="frozen in N strategies"
          >
            🧊{row.frozen_in_strategies}
          </span>
        )}
        {row.shadow_in_strategies > 0 && (
          <span
            className="inline-block text-[10px] text-purple-300"
            title="shadow in N strategies"
          >
            👻{row.shadow_in_strategies}
          </span>
        )}
        {row.frozen_in_strategies === 0 && row.shadow_in_strategies === 0 && (
          <span className="text-muted-foreground text-[10px]">—</span>
        )}
      </td>
    </tr>
  );
}
