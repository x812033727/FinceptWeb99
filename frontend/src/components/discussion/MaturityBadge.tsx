import { Clock } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { MaturitySignals, MaturityTier } from "./_helpers";

/**
 * PR-4a: render the strategy's lifecycle tier as a colored chip
 * with a tooltip that explains the inputs the rule engine used
 * (sample count, sweep count, recent vs baseline brier).
 *
 * Hides itself when `tier` is undefined so old strategies (rows
 * from before PR-4a landed) don't grow phantom UI before the
 * first sweep fires the maturity computation.
 */
export function MaturityBadge({
  tier,
  signals,
}: {
  tier?: MaturityTier;
  signals?: MaturitySignals;
}) {
  const { t } = useTranslation();
  if (!tier) return null;

  const tone =
    tier === "mature"
      ? "bg-success/40 text-success border-success/50"
      : tier === "learning"
        ? "bg-info/40 text-info border-info/50"
        : tier === "cold_start"
          ? "bg-slate-800/60 text-slate-300 border-slate-600/50"
          : tier === "drifting"
            ? "bg-warning/40 text-warning border-warning/50"
            : /* stale */ "bg-zinc-800/60 text-zinc-400 border-zinc-700/50";

  const icon =
    tier === "mature"
      ? "✓"
      : tier === "learning"
        ? "○"
        : tier === "cold_start"
          ? "·"
          : tier === "drifting"
            ? "⚠"
            : <Clock className="h-3 w-3" />;

  // Build a one-line tooltip from the signals so the operator
  // sees the WHY without clicking. Only include fields that have
  // values — empty 'samples=' makes the tooltip read worse, not
  // better.
  const lines: string[] = [];
  if (signals?.production_sweep_count !== undefined) {
    lines.push(
      `${t("discussion.maturity.tooltip.sweeps")}: ${signals.production_sweep_count}`,
    );
  }
  if (signals?.calibration_sample_count !== undefined && signals.calibration_sample_count !== null) {
    lines.push(
      `${t("discussion.maturity.tooltip.samples")}: ${signals.calibration_sample_count}`,
    );
  }
  if (
    signals?.recent_brier !== undefined && signals.recent_brier !== null
    && signals?.baseline_brier !== undefined && signals.baseline_brier !== null
  ) {
    lines.push(
      `${t("discussion.maturity.tooltip.brier")}: ${signals.recent_brier.toFixed(3)} vs ${signals.baseline_brier.toFixed(3)}`,
    );
  }
  if (signals?.brier_ratio !== undefined && signals.brier_ratio !== null) {
    lines.push(
      `${t("discussion.maturity.tooltip.ratio")}: ${signals.brier_ratio.toFixed(2)}`,
    );
  }
  const tooltip = lines.length > 0 ? lines.join(" · ") : t(`discussion.maturity.label.${tier}`);

  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 text-meta rounded border ${tone}`}
      title={tooltip}
    >
      <span>{icon}</span>
      <span>{t(`discussion.maturity.label.${tier}`)}</span>
    </span>
  );
}
