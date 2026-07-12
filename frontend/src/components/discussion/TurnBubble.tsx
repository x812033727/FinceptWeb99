import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Check, CornerDownRight, Edit3, X } from "lucide-react";
import type { Turn } from "@/types/discussion";
import { cn } from "@/lib/utils";
import { PersonaAvatar } from "./PersonaAvatar";
import { getPersonaIdentity } from "./personaIdentity";
import { renderInlineMarkdown } from "./_helpers";

interface TurnBubbleProps {
  turn: Turn;
  personaName: (id: string) => string;
  /** Optional per-persona short label (en: "BU"; zh: "巴"). */
  personaInitial?: string;
  /** Override body content — used by StreamingTurnCard to render
   *  `<TurnBubble>` with the live streaming buffer instead of the
   *  finalised `turn.content`. */
  bodyOverride?: ReactNode;
  /** Adds a subtle ring + glow to indicate the bubble is in-flight. */
  streaming?: boolean;
  className?: string;
}

// Stance-aware icon-only badge in the top-right corner. Replaces the
// inline coloured chip that used to crowd the persona name.
const STANCE_ICON: Record<
  Turn["stance"],
  { Icon: typeof Check; cls: string; titleKey: string }
> = {
  agree: {
    Icon: Check,
    cls: "bg-success/10 text-success ring-success/30",
    titleKey: "discussion.stance.agree",
  },
  dissent: {
    Icon: X,
    cls: "bg-danger/10 text-danger ring-danger/30",
    titleKey: "discussion.stance.dissent",
  },
  supplement: {
    Icon: CornerDownRight,
    cls: "bg-blue-900/30 text-blue-300 ring-blue-800/50",
    titleKey: "discussion.stance.supplement",
  },
  user_input: {
    Icon: Edit3,
    cls: "bg-warning/10 text-warning ring-warning/30",
    titleKey: "discussion.stance.user_input",
  },
};

export function TurnBubble({
  turn, personaName, personaInitial, bodyOverride, streaming, className,
}: TurnBubbleProps) {
  const { t } = useTranslation();
  const name = personaName(turn.persona_id);
  const identity = getPersonaIdentity(turn.persona_id, name, personaInitial);
  const stance = STANCE_ICON[turn.stance] ?? STANCE_ICON.supplement;
  const StanceIcon = stance.Icon;

  const isAgreeSilent =
    turn.stance === "agree" && !bodyOverride && !turn.content.trim();
  const body = isAgreeSilent ? t("discussion.agree_silent") : turn.content;

  // B4 badges: the owner's interjected question (`user_input` stance)
  // vs the persona turn answering it (`injected_by_user` on a normal
  // stance). Between-rounds injects and post-mortem prompts share the
  // question badge — they're all owner-authored turns.
  const injectedBadgeKey =
    turn.stance === "user_input"
      ? "discussion.turn_badge_user_question"
      : turn.injected_by_user
        ? "discussion.turn_badge_interject_reply"
        : null;

  return (
    <article
      className={cn(
        // Left-edge accent bar via border-l + per-persona hue.
        "relative bg-card border border-border rounded-lg p-3 sm:p-4 border-l-[3px] transition-shadow",
        streaming &&
          "ring-2 ring-primary/60 shadow-[0_0_20px_-6px_hsl(var(--primary))]",
        className,
      )}
      style={{ borderLeftColor: identity.accentColor }}
    >
      {/* Header — avatar + persona name + round, with stance icon
          floated to the corner so it doesn't crowd the name. */}
      <header className="flex items-start gap-2 mb-1.5">
        <PersonaAvatar
          personaId={turn.persona_id}
          name={name}
          initial={personaInitial}
        />
        <div className="flex-1 min-w-0">
          <p
            className="font-semibold text-sm leading-tight truncate"
            style={{ color: identity.accentColor }}
          >
            {name}
          </p>
          <p className="text-[10px] text-muted-foreground mt-0.5">
            R{turn.round}
          </p>
        </div>
        {injectedBadgeKey && (
          <span className="shrink-0 px-1.5 py-0.5 rounded-full text-[10px] border bg-warning/10 text-warning border-warning/30 self-center">
            {t(injectedBadgeKey)}
          </span>
        )}
        <span
          className={cn(
            "shrink-0 inline-flex items-center justify-center h-5 w-5 rounded ring-1",
            stance.cls,
          )}
          title={t(stance.titleKey)}
          aria-label={t(stance.titleKey)}
        >
          <StanceIcon className="h-3 w-3" aria-hidden="true" />
        </span>
      </header>

      {/* Body — width-capped via max-w-prose so long Chinese
          paragraphs don't stretch to the full container on
          desktop. Mobile widths are already < prose so this is a
          no-op there. */}
      <div className="text-sm text-foreground whitespace-pre-wrap leading-7 max-w-prose">
        {bodyOverride ?? renderInlineMarkdown(body)}
        {streaming && (
          <span
            className="inline-block w-1.5 h-3.5 bg-current ml-0.5 animate-pulse align-middle"
            aria-hidden="true"
          />
        )}
      </div>
    </article>
  );
}
