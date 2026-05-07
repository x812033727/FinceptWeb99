import { useTranslation } from "react-i18next";
import type { PostMortemDiff } from "@/types/discussion";

/**
 * PR-2: render the structured delta between the original conclusion
 * and the post-mortem revised one. Sits between the two
 * ConclusionCard variants on DiscussionPage so the operator can
 * skim "what changed" without comparing two cards by eye.
 *
 * Hides entirely when `diff` is null/undefined (no post-mortem ran,
 * either side parse-errored, or the discussion predates PR-2).
 */
export function ConclusionDiffCard({ diff }: { diff?: PostMortemDiff | null }) {
  const { t } = useTranslation();
  if (!diff) return null;

  const confidenceEntries = Object.entries(diff.confidence_changes ?? {});
  const overlapPct = Math.round((diff.reasoning_overlap ?? 0) * 100);
  // High overlap = post-mortem mostly preserved the narrative;
  // low overlap = significant rethink. Color-code so the operator
  // grasps the magnitude at a glance.
  const overlapColor =
    overlapPct >= 70
      ? "text-emerald-300"
      : overlapPct >= 40
        ? "text-amber-300"
        : "text-red-300";

  const consensusDelta = diff.consensus_score_delta ?? 0;
  const hasNoChanges =
    diff.symbols_added.length === 0
    && diff.symbols_removed.length === 0
    && confidenceEntries.length === 0
    && consensusDelta === 0
    && !diff.time_horizon_changed;

  return (
    <div
      className="bg-gradient-to-br from-purple-950/30 to-amber-950/20 border
                 border-purple-800/40 rounded-lg p-3 mt-4 mb-4"
    >
      <h4 className="text-xs font-semibold text-purple-200 mb-2">
        🔀 {t("discussion.diff.title")}
      </h4>

      {hasNoChanges && (
        <p className="text-[11px] text-muted-foreground italic">
          {t("discussion.diff.no_structural_change")}
        </p>
      )}

      {(diff.symbols_added.length > 0 || diff.symbols_removed.length > 0) && (
        <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
          {diff.symbols_added.length > 0 && (
            <span className="flex items-center gap-1">
              <span className="text-emerald-300 font-medium">
                + {t("discussion.diff.added")}:
              </span>
              {diff.symbols_added.map((s) => (
                <span
                  key={s}
                  className="font-mono px-1 py-0.5 rounded bg-emerald-900/30
                             text-emerald-200 border border-emerald-800/40"
                >
                  {s}
                </span>
              ))}
            </span>
          )}
          {diff.symbols_removed.length > 0 && (
            <span className="flex items-center gap-1">
              <span className="text-red-300 font-medium">
                − {t("discussion.diff.removed")}:
              </span>
              {diff.symbols_removed.map((s) => (
                <span
                  key={s}
                  className="font-mono px-1 py-0.5 rounded bg-red-950/30
                             text-red-200 border border-red-900/40"
                >
                  {s}
                </span>
              ))}
            </span>
          )}
        </div>
      )}

      {confidenceEntries.length > 0 && (
        <div className="mb-2">
          <div className="text-[11px] text-muted-foreground mb-1">
            {t("discussion.diff.confidence_shifts")}
          </div>
          <ul className="text-[11px] space-y-0.5">
            {confidenceEntries.map(([sym, change]) => {
              const positive = change.delta > 0;
              return (
                <li key={sym} className="flex items-center gap-2">
                  <span className="font-mono w-12 shrink-0">{sym}</span>
                  <span className="text-muted-foreground">
                    {change.orig.toFixed(2)} →{" "}
                    <span className={positive ? "text-emerald-300" : "text-red-300"}>
                      {change.post.toFixed(2)}
                    </span>
                  </span>
                  <span className={positive ? "text-emerald-400" : "text-red-400"}>
                    ({positive ? "+" : ""}
                    {change.delta.toFixed(2)})
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span>
          {t("discussion.diff.reasoning_overlap")}:{" "}
          <span className={overlapColor}>{overlapPct}%</span>
        </span>
        {consensusDelta !== 0 && (
          <span>
            {t("discussion.diff.consensus_delta")}:{" "}
            <span className={consensusDelta > 0 ? "text-emerald-300" : "text-red-300"}>
              {consensusDelta > 0 ? "+" : ""}
              {consensusDelta.toFixed(2)}
            </span>
          </span>
        )}
        {diff.time_horizon_changed && (
          <span className="text-amber-300">
            {t("discussion.diff.horizon_changed")}
          </span>
        )}
      </div>
    </div>
  );
}
