import { useTranslation } from "react-i18next";
import { useCollapsible } from "@/hooks/useCollapsible";
import type { Turn } from "@/types/discussion";
import { RoundDivider } from "./RoundDivider";
import { TurnBubble } from "./TurnBubble";

export interface RoundSectionProps {
  discussionId: string;
  round: number;
  turns: Turn[];
  personaName: (id: string) => string;
  /** Optional resolver for the per-persona avatar initial — i18n
   *  `personas.agents.<id>.short` lookup happens in the parent so the
   *  TurnBubble doesn't have to pull translations itself. */
  personaInitial?: (id: string) => string | undefined;
  /**
   * Override the default-folded behaviour for rounds the page wants
   * surfaced immediately (e.g. the latest round in the transcript).
   * Still respected by `useCollapsible`'s localStorage memory — the
   * user can manually collapse and that decision sticks.
   */
  defaultExpanded?: boolean;
}

/**
 * One collapsible block per round in the transcript. Replaces the
 * faint 1px divider + 11px label header with a centred RoundDivider
 * chip flanked by gradient lines (PR-C). Per-turn bubbles render via
 * `<TurnBubble>` which carries the per-persona accent bar + avatar.
 *
 * Default state:
 *   - Folded for ordinary persona-only rounds (per the user's
 *     "all rounds folded by default" UX from PR #247).
 *   - Auto-expanded when this round contains a `user_input` turn
 *     (post-mortem injection or mid-round inject).
 *   - Auto-expanded when the parent passes `defaultExpanded` (latest
 *     round, PR-B).
 *
 * State persists to localStorage keyed by (discussionId, round) so a
 * reload preserves manual expand / collapse.
 */
export function RoundSection({
  discussionId, round, turns, personaName, personaInitial, defaultExpanded,
}: RoundSectionProps) {
  const { t } = useTranslation();
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
    <section className="first:mt-0">
      {/* Round divider — collapsible. Click toggles open. The divider
          chip itself shows the round label + turn count; the badge
          (post-mortem / user-injection) appears immediately below
          when applicable so it stays visible even when the round is
          collapsed. */}
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        aria-controls={`round-${discussionId}-${round}`}
        className="w-full text-left group"
      >
        <RoundDivider round={round} turnCount={turns.length} />
      </button>
      {(postMortemTurn || hasUserInjection) && (
        <div className="-mt-3 mb-3 flex justify-center">
          <span
            className={
              postMortemTurn
                ? "px-2 py-0.5 text-[10px] rounded-full border bg-purple-900/30 text-purple-300 border-purple-800/50"
                : "px-2 py-0.5 text-[10px] rounded-full border bg-amber-900/30 text-amber-300 border-amber-800/50"
            }
          >
            {postMortemTurn
              ? t("discussion.post_mortem_round_badge")
              : t("discussion.user_injection_round_badge")}
          </span>
        </div>
      )}
      {open && (
        <div
          id={`round-${discussionId}-${round}`}
          className="space-y-3 mt-2"
        >
          {turns.map((tn, i) => (
            <TurnBubble
              key={`${tn.round}-${tn.turn_index}-${i}`}
              turn={tn}
              personaName={personaName}
              personaInitial={personaInitial?.(tn.persona_id)}
            />
          ))}
        </div>
      )}
    </section>
  );
}
