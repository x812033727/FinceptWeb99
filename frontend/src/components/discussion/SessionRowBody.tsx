/**
 * Body of one sessions-rail row (title / backtest scoreboard lines +
 * status meta). Extracted verbatim from `pages/DiscussionPage.tsx`
 * (PR-8 巨石頁拆分).
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { Discussion } from "@/types/discussion";
import { DiscussionStatusBadge } from "@/components/discussion/DiscussionStatusBadge";
import {
  BAND_LABELS,
  fetchScoreboard,
  formatDateShort,
  formatDiscussionTitle,
  pctClass,
  signedPct,
} from "@/components/discussion/_helpers";

// Lazy on-demand fallback for the sidebar scoreboard. The persisted
// `daily_close_prices` column on Discussion is filled by the daily
// `score_discussion_outcomes` cron (09:30 UTC). When that cron lags
// — or for same-day concluded discussions whose anchor is in the
// past but the cron hasn't ticked yet — the column is NULL and the
// row would otherwise render "-/-/-/-/-" indefinitely. The detail
// page's `/scoreboard` endpoint computes the same payload on demand
// (with a live OHLCV waterfall fallback), so we fire it here per row
// and merge into the `formatDiscussionTitle` input. Top-level
// component so each row keeps its own `useQuery` instance (hook
// rules + ESLint `react-hooks/static-components`).
export function SessionRowBody({ s }: { s: Discussion }) {
  const { t } = useTranslation();
  const todayIso = new Date().toISOString().slice(0, 10);
  const anchorIso = s.as_of_date ?? s.created_at?.slice(0, 10) ?? null;
  const anchorReached = anchorIso ? anchorIso <= todayIso : false;
  const needsLazy =
    !s.daily_close_prices &&
    !!s.conclusion?.recommended_symbols?.length &&
    anchorReached;

  const { data: lazy } = useQuery({
    queryKey: ["discussion-scoreboard-lazy", s.id],
    queryFn: () => fetchScoreboard(s.id),
    enabled: needsLazy,
    staleTime: 60 * 60 * 1000,
    retry: false,
  });

  const merged = useMemo(() => {
    if (!lazy) return s;
    const dailyDict: Record<string, (number | null)[]> = {
      ...(s.daily_close_prices ?? {}),
    };
    const opensDict: Record<string, number> = { ...(s.day1_open_prices ?? {}) };
    for (const r of lazy.rows ?? []) {
      if (Array.isArray(r.daily_closes)) dailyDict[r.symbol] = r.daily_closes;
      if (typeof r.day1_open === "number") opensDict[r.symbol] = r.day1_open;
    }
    return {
      ...s,
      daily_close_prices: Object.keys(dailyDict).length ? dailyDict : null,
      day1_open_prices: Object.keys(opensDict).length ? opensDict : null,
    };
  }, [s, lazy]);

  const tt = formatDiscussionTitle(merged);
  return (
    <>
      {tt.text !== undefined ? (
        <div className="line-clamp-2 font-bold text-foreground">{tt.text}</div>
      ) : (
        <div className="space-y-0.5">
          <div className="font-bold">{tt.date}</div>
          {tt.lines?.map((ln) => {
            const bandLabel = ln.band ? BAND_LABELS[ln.band] : undefined;
            return (
              <div key={ln.symbol} className="font-mono">
                <span className={bandLabel?.cls ?? ""}>{ln.symbol}</span>
                {bandLabel?.mark ? (
                  <span className={`ml-1 text-[10px] ${bandLabel.cls}`}>
                    {bandLabel.mark}
                  </span>
                ) : null}
                :{" "}
                {ln.changePcts.map((p, i) => (
                  <span key={i}>
                    <span className={pctClass(p)}>
                      {p !== null ? signedPct(p) : "—"}
                    </span>
                    {i < ln.changePcts.length - 1 ? "/" : ""}
                  </span>
                ))}
              </div>
            );
          })}
        </div>
      )}
      <div className="mt-1 flex items-center gap-2 text-[10px] flex-wrap">
        <DiscussionStatusBadge status={s.status} />
        <span className="text-muted-foreground">
          {s.as_of_date
            ? t("discussion.session_backtest_prefix", { date: s.as_of_date })
            : formatDateShort(s.updated_at || s.created_at)}
        </span>
        <span className="text-muted-foreground">·</span>
        <span className="text-muted-foreground">R{s.current_round}</span>
        <span className="text-muted-foreground">·</span>
        <span className="text-muted-foreground">
          {t("discussion.session_persona_count", { count: s.persona_ids.length })}
        </span>
      </div>
    </>
  );
}
