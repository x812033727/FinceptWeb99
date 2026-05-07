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
          className="inline-flex items-center px-2 py-0.5 text-[11px] rounded
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
            className="inline-flex items-center px-2 py-0.5 text-[11px] rounded
                       bg-emerald-900/30 text-emerald-300 border border-emerald-800/50"
            title={t("discussion.quality.passed_tooltip") as string}
          >
            ✓ {t("discussion.quality.passed")}
          </span>
        )}
        {hallucinations.length > 0 && (
          <button
            type="button"
            onClick={() => setHallucinationOpen((v) => !v)}
            className="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] rounded
                       bg-red-900/40 text-red-200 border border-red-700/60
                       hover:bg-red-900/60 transition-colors"
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
            className="inline-flex items-center px-2 py-0.5 text-[11px] rounded
                       bg-amber-900/40 text-amber-200 border border-amber-700/60"
            title={t("discussion.quality.contradiction_tooltip") as string}
          >
            ⚠ {t("discussion.quality.contradiction")}
          </span>
        )}
        {overConfident && (
          <span
            className="inline-flex items-center px-2 py-0.5 text-[11px] rounded
                       bg-amber-900/40 text-amber-200 border border-amber-700/60"
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
          className="text-[11px] text-red-200/90 bg-red-950/30 border
                     border-red-900/40 rounded px-2 py-1.5 space-y-0.5"
        >
          {hallucinations.map((w, i) => (
            <li key={`${w.round}-${w.persona_id}-${w.signal}-${i}`}>
              · R{w.round} <span className="font-mono">{w.persona_id}</span>{" "}
              引用了未在 ctx 內的 <span className="font-mono">{w.signal}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
