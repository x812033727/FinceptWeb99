import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { ToolCallEvent } from "./types";

interface ToolCallCardProps {
  call: ToolCallEvent;
  className?: string;
}

// Compact tool-call status card — used inline inside an assistant
// MessageBubble while a Claude Agent / OpenAI-compat toolset call
// is in flight, then again with the result once it lands. Status
// pill animates while running so the user knows something is
// happening even if the LLM hasn't streamed any text yet.
export function ToolCallCard({ call, className }: ToolCallCardProps) {
  const { t } = useTranslation();
  const statusColor =
    call.status === "running" ? "bg-warning animate-pulse" :
    call.status === "error" ? "bg-danger" :
    "bg-success";
  const argsStr = JSON.stringify(call.args, null, 2);
  return (
    <div className={cn("border border-border/60 bg-muted/30 rounded-md p-2 text-xs my-1.5", className)}>
      <div className="flex items-center gap-2">
        <span className={cn("inline-block w-2 h-2 rounded-full", statusColor)} />
        <span className="font-mono text-amber-300">{call.name}</span>
        <span className="text-muted-foreground">
          {call.status === "running"
            ? t("ai.tool.calling")
            : call.status === "error"
            ? t("ai.tool.failed")
            : t("ai.tool.done")}
        </span>
      </div>
      <details className="mt-1.5">
        <summary className="cursor-pointer text-muted-foreground hover:text-foreground select-none">
          {t("ai.tool.args")}
        </summary>
        <pre className="mt-1 bg-background/60 border border-border rounded p-2 overflow-auto max-h-40 text-foreground/80">
          {argsStr}
        </pre>
      </details>
      {call.result && (
        <details className="mt-1">
          <summary
            className={cn(
              "cursor-pointer hover:text-foreground select-none",
              call.isError ? "text-danger" : "text-muted-foreground"
            )}
          >
            {t("ai.tool.result")}
          </summary>
          <pre className="mt-1 bg-background/60 border border-border rounded p-2 overflow-auto max-h-60 text-foreground/80 whitespace-pre-wrap">
            {call.result}
          </pre>
        </details>
      )}
    </div>
  );
}
