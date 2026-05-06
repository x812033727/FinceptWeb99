import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";

type Status = "draft" | "running" | "done" | string;

const STYLES: Record<string, string> = {
  draft:   "border-muted-foreground/30 text-muted-foreground bg-muted/40",
  running: "border-amber-700/50 text-amber-300 bg-amber-900/20 animate-pulse",
  done:    "border-emerald-700/50 text-emerald-300 bg-emerald-900/20",
};

// Small status pill on each session button. Maps backend's
// `status` string (draft / running / done / archived) to a
// colour-coded chip so the user can scan the sessions list and
// tell which discussions are still in progress vs concluded
// without reading any text.
export function DiscussionStatusBadge({ status, className }: { status: Status; className?: string }) {
  const { t } = useTranslation();
  const cls = STYLES[status] ?? STYLES.draft;
  return (
    <span
      className={cn(
        "inline-flex items-center px-1.5 py-0.5 rounded border text-[10px] uppercase tracking-wider",
        cls,
        className
      )}
    >
      {t(`discussion.status.${status}`)}
    </span>
  );
}
