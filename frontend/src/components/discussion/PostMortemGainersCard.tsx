import { useTranslation } from "react-i18next";
import type { PostMortemResponse } from "./_helpers";

/**
 * Visual leaderboard for the post-mortem gainers (PR #257).
 *
 * Backend's `POST /post-mortem` returns the next-trading-day top-N
 * gainers as structured data; PR #249 only embedded them in the
 * injected user_input turn's markdown text. That's readable but not
 * scannable — operator can't sort, can't compare magnitudes
 * visually.
 *
 * This card surfaces the same data as a horizontal-bar leaderboard
 * with a symbol mini-quote so the operator can see at a glance
 * "the actual top gainers were 6505/2603/2317" without reading the
 * prose.
 *
 * Transient: data lives in mutation state. Page reload clears it
 * (the markdown in the user_input turn is the durable record).
 * Acceptable trade-off: the persona-facing prose is what matters
 * for the discussion; this card is operator situational awareness.
 */

export function PostMortemGainersCard({
  data,
}: {
  data: PostMortemResponse | null;
}) {
  const { t } = useTranslation();
  if (!data || data.top_gainers.length === 0) return null;

  // Bars scale to the largest absolute change in the set so the
  // top row always renders at full width and lower rows are
  // proportional. A single-row case (n=1) renders at full width
  // too — without the denominator falling back to 1 we'd get
  // NaN bar widths.
  const maxAbs = Math.max(
    1,
    ...data.top_gainers.map((g) => Math.abs(g.change_pct)),
  );

  return (
    <div className="bg-purple-950/15 border border-purple-800/40 rounded-lg p-3 mt-3 space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <h4 className="text-xs font-semibold text-purple-300">
          {t("discussion.post_mortem_gainers_title", {
            date: data.next_trading_day,
          })}
        </h4>
        <span className="text-[10px] text-muted-foreground">
          {t("discussion.post_mortem_gainers_count", {
            count: data.top_gainers.length,
          })}
        </span>
      </div>
      <ul className="space-y-1.5">
        {data.top_gainers.map((g, i) => {
          const sign = g.change_pct >= 0 ? "+" : "";
          const widthPct = (Math.abs(g.change_pct) / maxAbs) * 100;
          return (
            <li
              key={g.symbol}
              className="grid grid-cols-[2rem_minmax(0,5rem)_minmax(0,1fr)_auto] items-center gap-2 text-xs"
            >
              <span className="text-muted-foreground/70 tabular-nums">
                #{i + 1}
              </span>
              <span className="font-mono font-bold text-foreground">
                {g.symbol}
              </span>
              <div
                className="h-1.5 bg-secondary/40 rounded overflow-hidden"
                role="img"
                aria-label={`${g.symbol} ${sign}${g.change_pct.toFixed(2)}%`}
              >
                <div
                  className={`h-full ${
                    g.change_pct >= 0 ? "bg-emerald-500/70" : "bg-red-500/70"
                  }`}
                  style={{ width: `${widthPct}%` }}
                />
              </div>
              <span
                className={`font-mono tabular-nums text-right ${
                  g.change_pct >= 0 ? "text-emerald-400" : "text-red-400"
                }`}
              >
                {sign}{g.change_pct.toFixed(2)}%
              </span>
            </li>
          );
        })}
      </ul>
      <p className="text-[10px] text-muted-foreground/70 leading-relaxed">
        {t("discussion.post_mortem_gainers_help")}
      </p>
    </div>
  );
}
