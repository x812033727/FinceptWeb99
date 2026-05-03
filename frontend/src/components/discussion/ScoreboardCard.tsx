import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import type { ScoreboardResponse, ScoreboardRow } from "@/types/discussion";
import {
  fetchScoreboard,
  pctClass,
  signedPctSafe,
  toFixedSmart,
} from "./_helpers";

// Per recommended symbol, show D1-D5 close + change % vs day-1 open.
// Persisted by `tasks.score_discussion_outcomes` once the 5-day
// window expires; the API also computes on-demand for newer rows so
// the user sees partial data immediately.

function ScoreboardRowView({ row }: { row: ScoreboardRow }) {
  const { t } = useTranslation();
  return (
    <div className="border border-border rounded p-2 space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-sm text-foreground">{row.symbol}</span>
        <span className="text-[10px] text-muted-foreground">
          {t("discussion.scoreboard_day1_open")}：
          <span className="font-mono ml-1 text-foreground/80">
            {row.day1_open !== null ? toFixedSmart(row.day1_open) : "—"}
          </span>
        </span>
      </div>
      <div className="grid grid-cols-5 gap-1">
        {row.change_pcts.map((pct, i) => (
          <div
            key={i}
            className="text-center text-[10px] border border-border/40 rounded px-1 py-1"
          >
            <div className="text-muted-foreground">D{i + 1}</div>
            <div className={`font-mono font-semibold ${pctClass(pct)}`}>
              {signedPctSafe(pct)}
            </div>
            <div className="font-mono text-muted-foreground/70">
              {row.daily_closes[i] !== null
                ? toFixedSmart(row.daily_closes[i] as number)
                : "—"}
            </div>
          </div>
        ))}
      </div>
      {row.days_resolved < 5 && (
        <p className="text-[10px] text-muted-foreground/80">
          {t("discussion.scoreboard_pending", {
            resolved: row.days_resolved,
          })}
        </p>
      )}
    </div>
  );
}

export function ScoreboardCard({
  discussionId, hasConclusion,
}: {
  discussionId: string;
  hasConclusion: boolean;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  // Auto-expand the moment a conclusion lands so the user sees the
  // D1-D5 outcomes (especially in backtest mode where data is
  // immediately available). One-shot — only fires on the
  // `hasConclusion: false → true` transition, so a user who
  // manually collapses the card later won't have it spring back open.
  const prevHasConclusion = useRef(hasConclusion);
  useEffect(() => {
    if (!prevHasConclusion.current && hasConclusion) {
      setOpen(true);
    }
    prevHasConclusion.current = hasConclusion;
  }, [hasConclusion]);

  const { data, isLoading, isError, error } = useQuery<ScoreboardResponse>({
    queryKey: ["discussion-scoreboard", discussionId],
    queryFn: () => fetchScoreboard(discussionId),
    // 400 ("no conclusion") is reachable when this card is rendered
    // with hasConclusion=false; gate the request so axios doesn't
    // log the error to console for every card on the page.
    enabled: open && hasConclusion,
    staleTime: 60_000,
    retry: false,
  });

  return (
    <div className="bg-card border border-border rounded-lg p-3 mt-4">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 w-full text-left text-xs font-medium text-foreground hover:text-primary transition-colors"
        aria-expanded={open}
      >
        <span className="text-[9px] text-muted-foreground w-2.5 inline-block">
          {open ? "▼" : "▶"}
        </span>
        {t("discussion.scoreboard_title")}
        {data && data.rows.length > 0 && (
          <span className="ml-auto text-[10px] text-muted-foreground">
            {t("discussion.scoreboard_count", { n: data.rows.length })}
          </span>
        )}
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          <p className="text-[10px] text-muted-foreground leading-relaxed">
            {t("discussion.scoreboard_subtitle")}
          </p>
          {!hasConclusion ? (
            <p className="text-[10px] text-muted-foreground">
              {t("discussion.scoreboard_no_conclusion")}
            </p>
          ) : isLoading ? (
            <p className="text-[10px] text-muted-foreground animate-pulse">
              …
            </p>
          ) : isError ? (
            <p className="text-[10px] text-red-400">
              {(error as Error)?.message || t("discussion.scoreboard_error")}
            </p>
          ) : !data || data.rows.length === 0 ? (
            <p className="text-[10px] text-muted-foreground">
              {t("discussion.scoreboard_empty")}
            </p>
          ) : (
            data.rows.map((r) => <ScoreboardRowView key={r.symbol} row={r} />)
          )}
        </div>
      )}
    </div>
  );
}
