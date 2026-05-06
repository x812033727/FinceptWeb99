import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";

interface RoundDividerProps {
  round: number;
  /** Optional turn count to surface in the chip ("Round 2 · 4 turns"). */
  turnCount?: number;
  className?: string;
}

// Centred chip flanked by faded gradient lines. Replaces the old
// 11px label + 1px line so multi-round transcripts have an obvious
// section break instead of fading into the bubble stack.
export function RoundDivider({ round, turnCount, className }: RoundDividerProps) {
  const { t } = useTranslation();
  const label = turnCount != null
    ? t("discussion.round_summary", { round, count: turnCount })
    : t("discussion.round_label", { round });
  return (
    <div
      role="separator"
      aria-label={label}
      className={cn("flex items-center gap-3 my-6 first:mt-2", className)}
    >
      <span className="flex-1 h-px bg-gradient-to-r from-transparent to-border" />
      <span className="px-3 py-1 rounded-full bg-card border border-border text-[11px] font-semibold text-primary tracking-wider whitespace-nowrap">
        {label}
      </span>
      <span className="flex-1 h-px bg-gradient-to-l from-transparent to-border" />
    </div>
  );
}
