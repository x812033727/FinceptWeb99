import { useEffect } from "react";
import { useToastStore, type ToastSeverity } from "@/store/toastStore";

const severityStyles: Record<ToastSeverity, { bar: string; bg: string; icon: string }> = {
  info:    { bar: "bg-blue-500",   bg: "bg-blue-950/40 border-blue-900/50",   icon: "ⓘ" },
  success: { bar: "bg-green-500",  bg: "bg-green-950/40 border-green-900/50", icon: "✓" },
  warning: { bar: "bg-amber-500",  bg: "bg-amber-950/40 border-amber-900/50", icon: "!" },
  error:   { bar: "bg-red-500",    bg: "bg-red-950/40 border-red-900/50",     icon: "×" },
};

export default function Toaster() {
  const toasts = useToastStore((s) => s.toasts);
  const dismiss = useToastStore((s) => s.dismiss);

  useEffect(() => {
    const timers = toasts.map((t) =>
      window.setTimeout(() => dismiss(t.id), t.ttlMs),
    );
    return () => timers.forEach((id) => window.clearTimeout(id));
  }, [toasts, dismiss]);

  if (toasts.length === 0) return null;

  return (
    <div
      className="fixed top-4 right-4 z-[100] flex flex-col gap-2 max-w-sm w-[calc(100vw-2rem)]"
      role="region"
      aria-label="Notifications"
      aria-live="polite"
    >
      {toasts.map((t) => {
        const s = severityStyles[t.severity];
        return (
          <div
            key={t.id}
            className={`relative flex gap-3 items-start rounded-lg border ${s.bg} px-3 py-2.5 shadow-lg backdrop-blur-sm`}
            role={t.severity === "error" ? "alert" : "status"}
          >
            <span className={`mt-0.5 inline-flex w-5 h-5 items-center justify-center rounded-full text-xs font-bold text-white ${s.bar}`}>
              {s.icon}
            </span>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-foreground">{t.title}</p>
              {t.detail && (
                <p className="text-xs text-muted-foreground mt-0.5 break-words">{t.detail}</p>
              )}
            </div>
            <button
              onClick={() => dismiss(t.id)}
              className="text-muted-foreground hover:text-foreground text-base leading-none shrink-0"
              aria-label="Dismiss notification"
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}
