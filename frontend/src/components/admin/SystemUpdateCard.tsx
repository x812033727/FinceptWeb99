import { useCheckForUpdates, useTriggerUpdate, useVersion } from "@/hooks/useVersion";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";

export function SystemUpdateCard() {
  const { open, toggle } = useCollapsible("admin.system-update");
  const { data: version, isLoading } = useVersion();
  const check = useCheckForUpdates();
  const trigger = useTriggerUpdate();

  const status = trigger.data?.status;
  const message = trigger.data?.message;
  const checkError = check.isError;

  // Compact subtitle — current/latest version line, mirrored from the
  // expanded view so the collapsed card is still informative at a
  // glance ("you are on v0.5.12, latest is v0.5.12").
  const subtitle =
    isLoading || !version ? (
      <span className="animate-pulse">Checking GitHub…</span>
    ) : (
      <>
        Current <span className="font-mono">v{version.current}</span>
        {" · "}
        Latest <span className="font-mono">v{version.latest}</span>
        {version.update_available && (
          <span className="ml-2 text-amber-500">update available</span>
        )}
      </>
    );

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title="System update"
        subtitle={subtitle}
      />
      {open && (
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div className="space-y-1">
            {checkError && (
              <p className="text-xs text-red-500">Failed to reach GitHub. Try again.</p>
            )}
            {status && (
              <p
                className={`text-xs ${
                  status === "started"
                    ? "text-green-500"
                    : status === "failed"
                    ? "text-red-500"
                    : "text-muted-foreground"
                }`}
              >
                {status}: {message}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              disabled={check.isPending || trigger.isPending}
              onClick={() => check.mutate()}
              className="text-xs px-3 py-1.5 rounded border border-border bg-background hover:bg-accent/10 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {check.isPending ? "Checking…" : "Check for updates"}
            </button>
            <button
              disabled={!version?.update_available || trigger.isPending || check.isPending}
              onClick={() => trigger.mutate()}
              className="text-xs px-3 py-1.5 rounded border border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400 hover:bg-amber-500/20 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {trigger.isPending ? "Updating…" : "Update now"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
