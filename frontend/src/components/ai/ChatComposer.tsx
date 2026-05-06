import { useTranslation } from "react-i18next";
import { Send, Square } from "lucide-react";
import { cn } from "@/lib/utils";

interface ChatComposerProps {
  value: string;
  onChange: (next: string) => void;
  onSend: () => void;
  onStop: () => void;
  streaming: boolean;
  /** Disabled when no agent / target is selected. */
  disabled?: boolean;
  placeholder?: string;
  /** Small grey text under the textarea (e.g. "AI 回應僅供參考"). */
  disclaimerText?: string;
  className?: string;
}

// Bottom-of-page composer — textarea + send/stop button + optional
// disclaimer line. Enter sends, Shift+Enter inserts newline. Swaps
// to a red Stop button while streaming so the user has a clear
// abort affordance.
export function ChatComposer({
  value,
  onChange,
  onSend,
  onStop,
  streaming,
  disabled,
  placeholder,
  disclaimerText,
  className,
}: ChatComposerProps) {
  const { t } = useTranslation();
  const sendDisabled = disabled || !value.trim();
  return (
    <div className={cn("border-t border-border px-3 sm:px-6 py-3 sm:py-4 shrink-0", className)}>
      <div className="flex gap-2">
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (!streaming) onSend();
            }
          }}
          placeholder={placeholder}
          rows={2}
          disabled={streaming || disabled}
          className="flex-1 resize-none bg-card border border-border rounded-md px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary/50 disabled:opacity-50"
        />
        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            aria-label={t("ai.stop")}
            className="px-4 py-2 rounded-md bg-red-900/30 border border-red-800 text-red-400 text-sm hover:bg-red-900/50 transition-colors self-end min-h-[40px] inline-flex items-center gap-1.5"
          >
            <Square className="h-3.5 w-3.5 fill-current" aria-hidden="true" />
            <span className="hidden sm:inline">{t("ai.stop")}</span>
          </button>
        ) : (
          <button
            type="button"
            onClick={onSend}
            disabled={sendDisabled}
            aria-label={t("ai.send")}
            className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 self-end min-h-[40px] inline-flex items-center gap-1.5"
          >
            <Send className="h-3.5 w-3.5" aria-hidden="true" />
            <span className="hidden sm:inline">{t("ai.send")}</span>
          </button>
        )}
      </div>
      {disclaimerText && (
        <p className="text-xs text-muted-foreground mt-1.5">{disclaimerText}</p>
      )}
    </div>
  );
}
