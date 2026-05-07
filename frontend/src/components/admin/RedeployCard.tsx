import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import { useDeployStatus, useTriggerDeploy } from "@/hooks/useDeploy";
import type { DeployPhase } from "@/types/system";

const PHASE_ORDER: DeployPhase[] = [
  "pulling",
  "pausing",
  "reset_stale",
  "building",
  "restarting",
  "nginx",
  "verifying",
  "completed",
];

const TERMINAL_PHASES: DeployPhase[] = ["idle", "completed", "failed", "unknown"];

function isRunning(phase: DeployPhase): boolean {
  return !TERMINAL_PHASES.includes(phase);
}

function shortSha(sha?: string | null): string {
  return (sha ?? "").slice(0, 7);
}

export function RedeployCard() {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible("admin.redeploy");
  const status = useDeployStatus();
  const trigger = useTriggerDeploy();
  const [showLog, setShowLog] = useState(false);
  const [reloadCountdown, setReloadCountdown] = useState<number | null>(null);
  const lastTriggerIdRef = useRef<string | null>(null);
  const reloadFiredRef = useRef(false);

  const phase: DeployPhase = (status.data?.phase ?? "idle") as DeployPhase;
  const running = isRunning(phase);
  const data = status.data;

  // Auto-reload after a successful deploy that THIS browser triggered.
  // Guard with `lastTriggerIdRef` so a different admin's deploy
  // completing in the background doesn't yank the page out from
  // under us. `reloadFiredRef` makes the effect idempotent — useQuery
  // can re-emit the same `completed` payload several times before the
  // window.location.reload() takes effect.
  useEffect(() => {
    if (reloadFiredRef.current) return;
    if (phase !== "completed") return;
    if (!data?.trigger_id || data.trigger_id !== lastTriggerIdRef.current) return;
    if (!data.after_sha || data.after_sha === data.before_sha) return;
    reloadFiredRef.current = true;
    setReloadCountdown(2);
    const interval = setInterval(() => {
      setReloadCountdown((n) => {
        if (n === null) return null;
        if (n <= 1) {
          clearInterval(interval);
          window.location.reload();
          return 0;
        }
        return n - 1;
      });
    }, 1000);
    return () => clearInterval(interval);
  }, [phase, data?.trigger_id, data?.before_sha, data?.after_sha]);

  function handleClick() {
    if (running) return;
    if (!window.confirm(t("admin.redeploy.confirm_body"))) return;
    reloadFiredRef.current = false;
    setReloadCountdown(null);
    trigger.mutate(undefined, {
      onSuccess: (res) => {
        lastTriggerIdRef.current = res.trigger_id;
      },
    });
  }

  // Subtitle changes shape with phase so the collapsed card is still
  // informative ("running: building" / "completed: abc123 → def456").
  const subtitle = (() => {
    const branch = data?.branch ?? "main";
    if (running) {
      return t("admin.redeploy.subtitle_running", {
        phase: t(`admin.redeploy.phase.${phase}`),
      });
    }
    if (phase === "completed") {
      const when = data?.finished_at?.replace("T", " ").replace("Z", " UTC") ?? "";
      return t("admin.redeploy.subtitle_completed", {
        when,
        before: shortSha(data?.before_sha),
        after: shortSha(data?.after_sha),
      });
    }
    if (phase === "failed") {
      return t("admin.redeploy.subtitle_failed", {
        phase: t(`admin.redeploy.phase.${phase}`),
      });
    }
    return t("admin.redeploy.subtitle_idle", { branch });
  })();

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open}
        toggle={toggle}
        title={t("admin.redeploy.title")}
        subtitle={subtitle}
      />
      {open && (
        <div className="space-y-3">
          {/* Phase step indicator */}
          <div className="flex flex-wrap items-center gap-1 text-[10px]">
            {PHASE_ORDER.map((p, idx) => {
              const reached =
                PHASE_ORDER.indexOf(phase as DeployPhase) >= idx ||
                phase === "completed";
              const current = phase === p;
              const failedHere = phase === "failed" && current;
              return (
                <span
                  key={p}
                  className={`px-2 py-0.5 rounded border ${
                    failedHere
                      ? "border-red-500 bg-red-500/15 text-red-600"
                      : current
                      ? "border-amber-500 bg-amber-500/15 text-amber-600"
                      : reached
                      ? "border-green-500/40 bg-green-500/10 text-green-600"
                      : "border-border bg-background text-muted-foreground"
                  }`}
                >
                  {t(`admin.redeploy.phase.${p}`)}
                </span>
              );
            })}
          </div>

          {/* Metadata row */}
          {data && (
            <dl className="grid grid-cols-[120px_1fr] gap-x-3 gap-y-1 text-xs text-muted-foreground">
              {data.branch && (
                <>
                  <dt>{t("admin.redeploy.branch_label")}</dt>
                  <dd className="font-mono truncate">{data.branch}</dd>
                </>
              )}
              {data.actor && (
                <>
                  <dt>{t("admin.redeploy.actor_label")}</dt>
                  <dd className="font-mono truncate">{data.actor}</dd>
                </>
              )}
              {(data.before_sha || data.after_sha) && (
                <>
                  <dt>{t("admin.redeploy.sha_arrow_label")}</dt>
                  <dd className="font-mono">
                    {shortSha(data.before_sha) || "—"}
                    {" → "}
                    {shortSha(data.after_sha) || "…"}
                  </dd>
                </>
              )}
              {data.started_at && (
                <>
                  <dt>{t("admin.redeploy.started_at_label")}</dt>
                  <dd className="font-mono">{data.started_at}</dd>
                </>
              )}
              {data.finished_at && (
                <>
                  <dt>{t("admin.redeploy.finished_at_label")}</dt>
                  <dd className="font-mono">{data.finished_at}</dd>
                </>
              )}
            </dl>
          )}

          {/* Failure banner */}
          {phase === "failed" && data?.error && (
            <div className="rounded border border-red-500/40 bg-red-500/10 p-2 text-xs text-red-600 dark:text-red-400 space-y-1">
              <div className="font-medium">
                {t("admin.redeploy.error_label")}: {data.error}
              </div>
              {data.log_tail && data.log_tail.length > 0 && (
                <button
                  className="underline text-[11px]"
                  onClick={() => setShowLog((v) => !v)}
                >
                  {showLog
                    ? t("admin.redeploy.log_tail_hide")
                    : t("admin.redeploy.log_tail_show")}
                </button>
              )}
              {showLog && data.log_tail && (
                <pre className="mt-1 max-h-48 overflow-auto rounded bg-black/30 p-2 font-mono text-[10px] text-red-100 whitespace-pre-wrap">
                  {data.log_tail.join("\n")}
                </pre>
              )}
            </div>
          )}

          {/* Reload toast */}
          {reloadCountdown !== null && (
            <div className="rounded border border-green-500/40 bg-green-500/10 p-2 text-xs text-green-700 dark:text-green-400 flex items-center justify-between gap-2">
              <span>
                {t("admin.redeploy.reload_toast")} ({reloadCountdown}s)
              </span>
              <button
                className="underline"
                onClick={() => window.location.reload()}
              >
                {t("admin.redeploy.reload_now")}
              </button>
            </div>
          )}

          {/* Action button */}
          <div className="flex justify-end">
            <button
              disabled={running || trigger.isPending}
              onClick={handleClick}
              className="text-xs px-3 py-1.5 rounded border border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400 hover:bg-amber-500/20 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {running || trigger.isPending
                ? t("admin.redeploy.button_running")
                : t("admin.redeploy.button_label")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
