import { useTranslation } from "react-i18next";
import type { PostMortemResponse } from "./_helpers";

/**
 * Shows up beside ConclusionCard when the post-mortem was skipped
 * because the recommendation already cleared the win threshold.
 *
 * Renders the verdict's `winners` (which symbol(s) hit, when, and
 * by how much) so the operator immediately sees WHY no critique
 * round fired. Stays out of the way (collapsed-style green tint)
 * since this is a celebration, not a problem.
 */
export function PostMortemSkippedCard({
  data,
}: {
  data: PostMortemResponse | null | undefined;
}) {
  const { t } = useTranslation();
  if (!data || data.status !== "skipped" || !data.verdict) return null;
  const v = data.verdict;
  const best = v.best_pct;

  return (
    <div className="bg-up/10 border border-up/30 rounded-lg p-4 mt-4">
      <h3 className="text-sm font-semibold text-up mb-2">
        {t("discussion_post_mortem_skipped.title")}
      </h3>
      <p className="text-xs text-foreground/80 mb-2">
        {t("discussion_post_mortem_skipped.explanation", {
          window: v.window_days,
          threshold: v.threshold_pct,
        })}
      </p>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="bg-card/50 rounded p-2">
          <p className="text-muted-foreground">
            {t("discussion_post_mortem_skipped.threshold")}
          </p>
          <p className="font-mono tabular-nums">
            {v.threshold_pct.toFixed(2)}%
          </p>
        </div>
        <div className="bg-card/50 rounded p-2">
          <p className="text-muted-foreground">
            {t("discussion_post_mortem_skipped.best_pct")}
          </p>
          <p className="font-mono tabular-nums text-up">
            {best != null
              ? `${best >= 0 ? "+" : ""}${best.toFixed(2)}%`
              : "—"}
          </p>
        </div>
      </div>
      {v.winners.length > 0 && (
        <div className="mt-3">
          <p className="text-xs text-muted-foreground mb-1">
            {t("discussion_post_mortem_skipped.winners_label")}
          </p>
          <ul className="space-y-1">
            {v.winners.map((w) => (
              <li
                key={w.symbol}
                className="flex items-center gap-2 text-xs"
              >
                <span className="font-mono px-1.5 py-0.5 bg-up/10 text-up rounded">
                  {w.symbol}
                </span>
                <span className="font-mono tabular-nums text-up">
                  {w.peak_pct >= 0 ? "+" : ""}
                  {w.peak_pct.toFixed(2)}%
                </span>
                <span className="text-muted-foreground">@</span>
                <span className="font-mono text-muted-foreground">
                  {w.peak_day}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="text-meta text-muted-foreground mt-3">
        {t("discussion_post_mortem_skipped.reason_prefix")}: {v.reason}
      </p>
    </div>
  );
}
