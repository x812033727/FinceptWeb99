import { useTranslation } from "react-i18next";
import { useCollapsible } from "@/hooks/useCollapsible";
import type { Turn } from "@/types/discussion";
import { STANCE_BADGE, renderInlineMarkdown } from "./_helpers";

export interface RoundSectionProps {
  discussionId: string;
  round: number;
  turns: Turn[];
  personaName: (id: string) => string;
  /**
   * Override the default-folded behaviour for rounds the page wants
   * surfaced immediately (e.g. the latest round in the transcript).
   * Still respected by `useCollapsible`'s localStorage memory — the
   * user can manually collapse and that decision sticks.
   */
  defaultExpanded?: boolean;
}

/**
 * One collapsible block per round in the transcript. Header shows
 * round number + persona-turn count + a chevron; body renders only
 * when expanded.
 *
 * Default state:
 *   - Folded for ordinary persona-only rounds (per the user's
 *     "all rounds folded by default" UX from PR #247).
 *   - Auto-expanded for rounds that contain a `_user/user_input`
 *     turn (e.g. the post-mortem self-critique injection from
 *     PR #249), AND a 「📋 事後檢討」 / 「✎ 插話」 badge in the
 *     header.
 *   - Auto-expanded when the parent passes `defaultExpanded` (used
 *     for the latest round so newcomers see something instead of a
 *     wall of folded headers — PR-B redesign).
 *
 * State persists to localStorage keyed by (discussionId, round),
 * so a reload preserves whatever the user manually expanded /
 * collapsed.
 */
export function RoundSection({
  discussionId, round, turns, personaName, defaultExpanded,
}: RoundSectionProps) {
  const { t } = useTranslation();
  // Detect post-mortem injection in this round so the header can
  // surface the badge and the section can default-expand.
  const postMortemTurn = turns.find(
    (tn) =>
      tn.stance === "user_input" &&
      (tn.content || "").trimStart().startsWith("【事後檢討"),
  );
  const hasUserInjection = turns.some((tn) => tn.stance === "user_input");
  const { open, toggle } = useCollapsible(
    `discussion.${discussionId}.round.${round}`,
    Boolean(hasUserInjection || defaultExpanded),
  );
  return (
    <div className="my-3 first:mt-0">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="w-full flex items-center gap-2 text-left hover:opacity-80 transition-opacity py-1"
      >
        <span className="text-[10px] text-muted-foreground w-3 inline-block">
          {open ? "▼" : "▶"}
        </span>
        <span className="text-[11px] font-semibold text-primary tracking-wider">
          {t("discussion.round_label", { round })}
        </span>
        <span className="text-[10px] text-muted-foreground">
          ({t("discussion.turn_count", { count: turns.length })})
        </span>
        {postMortemTurn ? (
          <span className="px-1.5 py-0.5 text-[10px] rounded border bg-purple-900/30 text-purple-300 border-purple-800/50">
            {t("discussion.post_mortem_round_badge")}
          </span>
        ) : hasUserInjection ? (
          <span className="px-1.5 py-0.5 text-[10px] rounded border bg-amber-900/30 text-amber-300 border-amber-800/50">
            {t("discussion.user_injection_round_badge")}
          </span>
        ) : null}
        <span className="flex-1 h-px bg-border" />
      </button>
      {open && (
        <div className="space-y-2 mt-2">
          {turns.map((tn, i) => {
            const badge = STANCE_BADGE[tn.stance] ?? STANCE_BADGE.supplement;
            const body =
              tn.stance === "agree" && !tn.content.trim()
                ? t("discussion.agree_silent")
                : tn.content;
            return (
              <div
                key={`${tn.round}-${tn.turn_index}-${i}`}
                className="bg-card border border-border rounded-lg p-3"
              >
                <div className="flex items-center gap-2 text-xs text-muted-foreground mb-1">
                  <span className="font-bold text-red-500">
                    {personaName(tn.persona_id)}
                  </span>
                  <span>·</span>
                  <span>R{tn.round}</span>
                  <span>·</span>
                  <span className={`px-1.5 py-0.5 rounded border text-[10px] ${badge.cls}`}>
                    {badge.label}
                  </span>
                </div>
                <div className="text-sm text-foreground whitespace-pre-wrap leading-relaxed">
                  {renderInlineMarkdown(body)}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
