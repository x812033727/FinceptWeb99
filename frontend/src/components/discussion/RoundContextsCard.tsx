import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import type { RoundContextSnapshot } from "@/types/discussion";
import {
  fetchRoundContexts,
  formatCompactNumber,
  signedPct,
  summarizeContext,
  toFixedSmart,
} from "./_helpers";

// Surfaces the per-round `gather_market_context` snapshots persisted
// by PR #135. Lets the user audit "what data the personas saw at
// the time" instead of re-deriving from current market state.
//
// Each round renders a small headline summary (大盤 / 新聞 / 外資 /
// 月營收) plus a collapsible raw-JSON block for power users / debug.
// Initially collapsed — the section is opt-in audit material, not
// primary reading on every visit.

function RoundContextRow({ snap }: { snap: RoundContextSnapshot }) {
  const { t, i18n } = useTranslation();
  const [showJson, setShowJson] = useState(false);
  const summary = useMemo(() => summarizeContext(snap.context), [snap.context]);

  const taiexLine = summary.taiex_value != null
    ? `TAIEX ${toFixedSmart(summary.taiex_value)}${
        summary.taiex_history_change_pct != null
          ? ` (${signedPct(summary.taiex_history_change_pct)})`
          : ""
      }`
    : null;

  const newsLine = summary.news_bullish != null || summary.news_bearish != null
    ? `${t("discussion.context_news_label")}：` +
      `${t("discussion.context_bullish")} ${summary.news_bullish ?? 0} / ` +
      `${t("discussion.context_bearish")} ${summary.news_bearish ?? 0}`
    : summary.backtest_news_unavailable
      ? `${t("discussion.context_news_label")}：${t("discussion.context_backtest_news_unavailable")}`
      : null;

  const buyerLine = summary.top_foreign_buyer
    ? `${t("discussion.context_top_foreign")}：${summary.top_foreign_buyer.symbol}` +
      (summary.top_foreign_buyer.industry
        ? ` (${summary.top_foreign_buyer.industry})`
        : "") +
      (summary.top_foreign_buyer.net != null
        ? ` ${formatCompactNumber(summary.top_foreign_buyer.net)}`
        : "")
    : null;

  const growerLine = summary.top_revenue_grower
    ? `${t("discussion.context_top_revenue")}：${summary.top_revenue_grower.symbol}` +
      (summary.top_revenue_grower.industry
        ? ` (${summary.top_revenue_grower.industry})`
        : "") +
      (summary.top_revenue_grower.yoy != null
        ? ` YoY ${signedPct(summary.top_revenue_grower.yoy / 100)}`
        : "")
    : null;

  const macroLine = summary.macro_summary
    ? `${t("discussion.context_macro")}：${summary.macro_summary}`
    : null;

  const userCtxLine = summary.user_context_summary
    ? `${t("discussion.context_user")}：${summary.user_context_summary}`
    : null;

  const priorLine = summary.prior_discussions_summary
    ? `${t("discussion.context_prior")}：${summary.prior_discussions_summary}`
    : null;

  return (
    <div className="border border-border rounded p-2 space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold text-primary">
          {t("discussion.round_label", { round: snap.round })}
        </span>
        <span className="text-[10px] text-muted-foreground">
          {new Date(snap.captured_at).toLocaleString(i18n.language, {
            month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit",
          })}
        </span>
      </div>
      <div className="space-y-0.5 text-[11px] text-foreground/80">
        {taiexLine && <div>{taiexLine}</div>}
        {newsLine && <div>{newsLine}</div>}
        {buyerLine && <div>{buyerLine}</div>}
        {growerLine && <div>{growerLine}</div>}
        {macroLine && <div>{macroLine}</div>}
        {summary.focus_briefs_summary?.map((line, i) => (
          <div key={`fb-${i}`} className="text-amber-400/80">
            {t("discussion.context_focus")}：{line}
          </div>
        ))}
        {userCtxLine && <div className="text-emerald-400/80">{userCtxLine}</div>}
        {priorLine && <div className="text-blue-400/80">{priorLine}</div>}
      </div>
      <button
        type="button"
        onClick={() => setShowJson((v) => !v)}
        className="text-[10px] text-muted-foreground hover:text-primary"
      >
        {showJson
          ? t("discussion.context_hide_json")
          : t("discussion.context_show_json")}
      </button>
      {showJson && (
        <pre className="mt-1 text-[10px] bg-card/40 border border-border rounded p-2 overflow-x-auto max-h-64 leading-tight">
          {JSON.stringify(snap.context, null, 2)}
        </pre>
      )}
    </div>
  );
}

export function RoundContextsCard({ discussionId }: { discussionId: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  // Lazy-load the snapshots: don't burn the request until the user
  // expands the card, since the JSON blob can be ~25KB and most
  // visitors come to read the transcript, not audit the data.
  const { data: snaps = [], isLoading } = useQuery<RoundContextSnapshot[]>({
    queryKey: ["discussion-round-contexts", discussionId],
    queryFn: () => fetchRoundContexts(discussionId),
    enabled: open,
    staleTime: 60_000,
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
        {t("discussion.context_replay_title")}
        {snaps.length > 0 && (
          <span className="ml-auto text-[10px] text-muted-foreground">
            {t("discussion.context_replay_count", { n: snaps.length })}
          </span>
        )}
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          <p className="text-[10px] text-muted-foreground leading-relaxed">
            {t("discussion.context_replay_subtitle")}
          </p>
          {isLoading ? (
            <p className="text-[10px] text-muted-foreground animate-pulse">
              …
            </p>
          ) : snaps.length === 0 ? (
            <p className="text-[10px] text-muted-foreground">
              {t("discussion.context_replay_empty")}
            </p>
          ) : (
            snaps.map((s) => <RoundContextRow key={s.round} snap={s} />)
          )}
        </div>
      )}
    </div>
  );
}
