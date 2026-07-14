import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { QualitySignals } from "@/types/discussion";

/**
 * PR-1: surface the synthesizer's post-parse quality signals as inline
 * badges above the conclusion body. Three warning kinds (hallucinated
 * values, consensus contradiction, over-confidence) render as
 * yellow/red chips; a clean transcript renders a single green
 * "passed" chip so the operator sees positive reinforcement instead
 * of an empty area when everything is fine.
 *
 * Hides entirely when `signals` is undefined (pre-PR-1 conclusions)
 * so old discussions don't grow phantom UI.
 */
export function QualitySignalsRow({ signals }: { signals?: QualitySignals }) {
  const { t } = useTranslation();
  const [hallucinationOpen, setHallucinationOpen] = useState(false);

  if (!signals) return null;

  // Parse-error placeholder — distinct from "no warnings" so the
  // operator knows the audit was skipped, not that everything is fine.
  if (signals._skipped) {
    return (
      <div className="flex flex-wrap gap-1.5 mb-3">
        <span
          className="inline-flex items-center px-2 py-0.5 text-meta rounded
                     bg-muted/40 text-muted-foreground border border-border"
        >
          {t("discussion.quality.skipped")}
        </span>
      </div>
    );
  }

  const hallucinations = signals.hallucination_warnings ?? [];
  const overConfident = !!signals.confidence_stats?.over_confident;
  const contradiction = !!signals.consensus_contradiction;
  const cleanRun =
    hallucinations.length === 0 && !overConfident && !contradiction;

  return (
    <div className="flex flex-col gap-1.5 mb-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {cleanRun && (
          <span
            className="inline-flex items-center px-2 py-0.5 text-meta rounded
                       bg-success/10 text-success border border-success/30"
            title={t("discussion.quality.passed_tooltip") as string}
          >
            ✓ {t("discussion.quality.passed")}
          </span>
        )}
        {hallucinations.length > 0 && (
          <button
            type="button"
            onClick={() => setHallucinationOpen((v) => !v)}
            className="inline-flex items-center gap-1 px-2 py-0.5 text-meta rounded
                       bg-danger/15 text-danger border border-danger/30
                       hover:bg-danger/25 transition-colors"
          >
            ⚠ {t("discussion.quality.hallucination_count", {
              count: hallucinations.length,
            })}
            <span className="text-[9px] opacity-70">
              {hallucinationOpen ? "▼" : "▶"}
            </span>
          </button>
        )}
        {contradiction && (
          <span
            className="inline-flex items-center px-2 py-0.5 text-meta rounded
                       bg-warning/15 text-warning border border-warning/30"
            title={t("discussion.quality.contradiction_tooltip") as string}
          >
            ⚠ {t("discussion.quality.contradiction")}
          </span>
        )}
        {overConfident && (
          <span
            className="inline-flex items-center px-2 py-0.5 text-meta rounded
                       bg-warning/15 text-warning border border-warning/30"
            title={
              t("discussion.quality.over_confident_tooltip", {
                mean: signals.confidence_stats?.mean?.toFixed(2) ?? "?",
              }) as string
            }
          >
            ⚠ {t("discussion.quality.over_confident")}
          </span>
        )}
      </div>
      {hallucinationOpen && hallucinations.length > 0 && (
        <ul
          className="text-meta text-danger/90 bg-danger/10 border
                     border-danger/30 rounded px-2 py-1.5 space-y-0.5"
        >
          {hallucinations.map((w, i) => (
            <li key={`${w.round}-${w.persona_id}-${w.signal}-${i}`}>
              · R{w.round} <span className="font-mono">{w.persona_id}</span>{" "}
              {t("discussion.quality_hallucination_cited")} <span className="font-mono">{w.signal}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
