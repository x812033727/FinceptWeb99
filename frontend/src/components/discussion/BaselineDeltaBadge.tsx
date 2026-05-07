import { useTranslation } from "react-i18next";
import type { Conclusion } from "@/types/discussion";

/**
 * PR-5c: render the conclusion's `vs_baseline` block as a chip
 * row above the recommendations. Two pieces:
 *
 *   - consensus_pct_change → "本場 consensus 比 90 天平均 +N%"
 *     coloured green when positive, amber when negative
 *   - brier comparison → since the verifier hasn't graded this
 *     conclusion yet, the chip says "待 5 日後驗證"
 *
 * Hides itself entirely when `vs_baseline` is missing (live
 * discussions, parse errors, cold-start strategies) so the
 * conclusion card stays clean for the typical operator path.
 */
export function BaselineDeltaBadge({
  conclusion,
}: {
  conclusion: Conclusion;
}) {
  const { t } = useTranslation();
  const vb = conclusion.vs_baseline;
  if (!vb) return null;

  const pctChange = vb.consensus_pct_change;
  const consensusBaseline = vb.consensus_baseline;
  const brierBaseline = vb.brier_baseline;

  // No comparison data at all → don't render (e.g. brand-new
  // strategy with no baseline yet)
  if (
    pctChange === null
    && consensusBaseline === null
    && brierBaseline === null
  ) {
    return null;
  }

  const consensusTone =
    pctChange === null
      ? "bg-muted/40 text-muted-foreground border-border"
      : pctChange >= 5
        ? "bg-emerald-900/40 text-emerald-200 border-emerald-700/50"
        : pctChange <= -5
          ? "bg-amber-900/40 text-amber-200 border-amber-700/50"
          : "bg-muted/40 text-muted-foreground border-border";

  return (
    <div className="flex flex-wrap items-center gap-1.5 mb-3">
      {pctChange !== null && consensusBaseline !== null && (
        <span
          className={`inline-flex items-center px-2 py-0.5 text-[11px] rounded border ${consensusTone}`}
          title={t("discussion.baseline.consensus_tooltip", {
            current: vb.consensus_score.toFixed(2),
            baseline: consensusBaseline.toFixed(2),
          }) as string}
        >
          {t("discussion.baseline.consensus_label")}:{" "}
          {pctChange >= 0 ? "+" : ""}
          {pctChange.toFixed(0)}%
        </span>
      )}
      {brierBaseline !== null && (
        <span
          className="inline-flex items-center px-2 py-0.5 text-[11px] rounded
                     bg-blue-900/30 text-blue-200 border border-blue-700/50"
          title={t("discussion.baseline.brier_tooltip", {
            baseline: brierBaseline.toFixed(3),
          }) as string}
        >
          {vb.verify_pending
            ? t("discussion.baseline.brier_pending")
            : t("discussion.baseline.brier_label")}
        </span>
      )}
    </div>
  );
}
