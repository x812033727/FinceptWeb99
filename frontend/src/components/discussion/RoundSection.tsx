import { useTranslation } from "react-i18next";
import { useCollapsible } from "@/components/Collapsible";
import type { Turn } from "@/types/discussion";
import { STANCE_BADGE, renderInlineMarkdown } from "./_helpers";

export interface RoundSectionProps {
  discussionId: string;
  round: number;
  turns: Turn[];
  personaName: (id: string) => string;
}

/**
 * One collapsible block per round in the transcript. Header always
 * shows round number + persona-turn count + a chevron; body renders
 * only when expanded. Default-collapsed (per the user's "all rounds
 * folded by default" UX) with state persisted to localStorage so a
 * reload preserves whatever the user expanded last session.
 *
 * Keyed by `(discussionId, round)` so two different discussions
 * don't cross-pollinate collapse state.
 *
 * Extracted from `pages/DiscussionPage.tsx` (PR #264) for unit-
 * testability — the parent page is a 1000-line orchestration shell
 * and pulling RoundSection out lets the collapse / chevron / stance
 * badge logic be tested in isolation.
 */
export function RoundSection({
  discussionId, round, turns, personaName,
}: RoundSectionProps) {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible(
    `discussion.${discussionId}.round.${round}`,
    false,
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
