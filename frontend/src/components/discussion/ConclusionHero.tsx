import { useTranslation } from "react-i18next";
import { AlertTriangle } from "lucide-react";
import type { Conclusion, DiscussionDetail } from "@/types/discussion";
import { cn } from "@/lib/utils";
import { renderInlineMarkdown } from "./_helpers";

interface ConclusionHeroProps {
  detail: DiscussionDetail;
  conclusion: Conclusion;
  variant: "primary" | "post_mortem";
  /** Click handler for recommended-symbol chips — wired by the parent
   *  shell to its `askPersonaAbout(symbol)` so the symbol chip stays
   *  interactive for deep-dive. */
  onSymbolClick?: (symbol: string) => void;
  className?: string;
}

// Hero body for ConclusionCard. Adds:
//   - per-variant gradient (amber for primary, purple for post-mortem)
//   - recommended-symbol chips with D1 micro-gauge from
//     detail.daily_close_prices when scoreboard data has resolved
//   - reasoning capped to max-w-prose with relaxed line-height
//   - risks list with AlertTriangle icon bullets
//   - stat row at the bottom (consensus / horizon)
//
// All interactive behaviour (collapse, export, ask-persona action
// buttons, parse-error fallback) stays in the parent ConclusionCard
// shell; this is purely the presentation of the success path.
export function ConclusionHero({
  detail, conclusion, variant, onSymbolClick, className,
}: ConclusionHeroProps) {
  const { t } = useTranslation();
  const isPostMortem = variant === "post_mortem";
  const closes = detail.daily_close_prices ?? null;

  // For each recommended symbol, return the D1 close-vs-anchor change
  // % so the chip can show a quick "+1.2%" gauge below the ticker.
  // The first close in the array is taken as D1 (PR #140 contract);
  // null entries (data not yet resolved) render as a dash.
  function d1Pct(symbol: string): number | null | undefined {
    if (!closes) return undefined;
    const arr = closes[symbol];
    if (!arr || arr.length === 0) return undefined;
    const first = arr[0];
    if (first == null) return null;
    // The close array stores absolute prices, not pcts. Without an
    // explicit anchor we can't compute D1 change pct here, so just
    // surface the raw close as a tertiary signal. The Scoreboard
    // card carries the full D1-D5 change-pct grid for serious
    // analysis.
    return first;
  }

  return (
    <div className={cn("space-y-3", className)}>
      {conclusion.recommended_symbols.length > 0 && (
        <div>
          <p className="text-micro uppercase tracking-wider text-muted-foreground mb-1.5">
            {t("discussion.recommended_symbols")}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {conclusion.recommended_symbols.map((s) => {
              const close = d1Pct(s);
              const name = conclusion.symbol_names?.[s];
              return (
                <button
                  key={s}
                  type="button"
                  onClick={() => onSymbolClick?.(s)}
                  title={
                    name
                      ? `${name} (${s}) — ${t("discussion.click_for_deep_dive")}`
                      : t("discussion.click_for_deep_dive")
                  }
                  className={cn(
                    "inline-flex flex-col items-start px-2.5 py-1.5 rounded text-xs transition-colors min-h-[40px]",
                    isPostMortem
                      ? "bg-purple-900/40 text-purple-100 hover:bg-purple-900/70 ring-1 ring-purple-800/60"
                      : "bg-amber-900/40 text-amber-100 hover:bg-amber-900/70 ring-1 ring-amber-800/60",
                  )}
                >
                  {name ? (
                    <>
                      <span className="font-semibold leading-tight">{name}</span>
                      <span className="font-mono text-[9px] opacity-70 leading-tight">
                        {s}
                      </span>
                    </>
                  ) : (
                    <span className="font-semibold font-mono">{s}</span>
                  )}
                  <span className="font-mono text-[9px] opacity-70 leading-tight">
                    {close == null
                      ? "D1 —"
                      : close === undefined
                        ? "D1 —"
                        : `D1 ${close.toFixed(2)}`}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      <p className="text-sm text-foreground whitespace-pre-wrap leading-7 max-w-prose">
        {renderInlineMarkdown(conclusion.reasoning)}
      </p>

      {conclusion.risks.length > 0 && (
        <div>
          <p className="text-micro uppercase tracking-wider text-muted-foreground mb-1.5">
            {t("discussion.risks")}
          </p>
          <ul className="space-y-1">
            {conclusion.risks.map((r, idx) => (
              <li
                key={idx}
                className="flex items-start gap-2 text-xs text-foreground/85 leading-relaxed max-w-prose"
              >
                <AlertTriangle
                  className={cn(
                    "h-3.5 w-3.5 mt-0.5 shrink-0",
                    isPostMortem ? "text-purple-300" : "text-amber-300",
                  )}
                  aria-hidden="true"
                />
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Stat row — consensus % + horizon. tabular-nums so multi-row
          renders align vertically when ConclusionHero is rendered
          twice (post-mortem alongside primary). */}
      <div className="flex items-center gap-4 pt-2 border-t border-border/60 text-[11px] text-muted-foreground tabular-nums">
        <span>
          {t("discussion.consensus")}：
          <span className="font-mono text-foreground ml-1">
            {(conclusion.consensus_score * 100).toFixed(0)}%
          </span>
        </span>
        <span>
          {t("discussion.horizon")}：
          <span className="text-foreground ml-1">
            {t(`discussion.horizon_${conclusion.time_horizon}`)}
          </span>
        </span>
        {detail.as_of_date && (
          <span className="ml-auto">
            {t("discussion.as_of_date")}：
            <span className="font-mono text-foreground ml-1">
              {detail.as_of_date}
            </span>
          </span>
        )}
      </div>
    </div>
  );
}

