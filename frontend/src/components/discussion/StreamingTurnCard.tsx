import { useTranslation } from "react-i18next";
import { Sparkles } from "lucide-react";
import { ToolCallCard } from "@/components/ai/ToolCallCard";
import { cn } from "@/lib/utils";
import { PersonaAvatar } from "./PersonaAvatar";
import { getPersonaIdentity } from "./personaIdentity";
import { renderInlineMarkdown } from "./_helpers";

interface StreamingToolEvent {
  id: string;
  kind: "call" | "result";
  name: string;
  args?: unknown;
  summary?: string;
  is_error?: boolean;
}

interface StreamingTurnCardProps {
  personaId: string;
  personaName: string;
  /** Optional explicit short avatar glyph from i18n. */
  personaInitial?: string;
  round: number | null;
  streamBuffer: string;
  toolEvents: StreamingToolEvent[];
  className?: string;
}

// Live streaming bubble. Distinct from a finalised TurnBubble via:
//   - primary ring + glow shadow (vs neutral border)
//   - "💭 思考中" chip in the header (animate-pulse)
//   - shared <ToolCallCard> for live tool-use events (PR-A primitive)
//   - blinking cursor at the buffer's tail
// Re-uses the same per-persona accent bar + avatar so users still
// recognise who's speaking.
export function StreamingTurnCard({
  personaId, personaName, personaInitial, round, streamBuffer,
  toolEvents, className,
}: StreamingTurnCardProps) {
  const { t } = useTranslation();
  const identity = getPersonaIdentity(personaId, personaName, personaInitial);
  return (
    <article
      className={cn(
        "relative bg-card border border-border rounded-lg p-3 sm:p-4 border-l-[3px] ring-2 ring-primary/60 shadow-[0_0_24px_-6px_hsl(var(--primary))]",
        className,
      )}
      style={{ borderLeftColor: identity.accentColor }}
      aria-busy="true"
    >
      <header className="flex items-start gap-2 mb-2">
        <PersonaAvatar
          personaId={personaId}
          name={personaName}
          initial={personaInitial}
        />
        <div className="flex-1 min-w-0">
          <p
            className="font-semibold text-sm leading-tight truncate"
            style={{ color: identity.accentColor }}
          >
            {personaName}
          </p>
          <p className="text-micro text-muted-foreground mt-0.5">
            {round != null ? `R${round}` : " "}
          </p>
        </div>
        <span className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-primary/15 text-primary text-micro font-medium animate-pulse">
          <Sparkles className="h-3 w-3" aria-hidden="true" />
          {t("discussion.thinking_chip")}
        </span>
      </header>

      {toolEvents.length > 0 && (
        <div className="mb-2 space-y-1">
          {toolEvents.map((ev) => (
            <ToolCallCard
              key={ev.id}
              call={{
                id: ev.id,
                name: ev.name,
                args: ev.args ?? {},
                result: ev.summary,
                isError: Boolean(ev.is_error),
                status:
                  ev.kind === "call"
                    ? "running"
                    : ev.is_error
                      ? "error"
                      : "done",
              }}
            />
          ))}
        </div>
      )}

      <div className="text-sm text-foreground whitespace-pre-wrap leading-7 max-w-prose">
        {renderInlineMarkdown(streamBuffer)}
        <span
          className="inline-block w-1.5 h-3.5 bg-current ml-0.5 animate-pulse align-middle"
          aria-hidden="true"
        />
      </div>
    </article>
  );
}
