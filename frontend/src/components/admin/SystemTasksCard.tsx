import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import type {
  SystemTaskConfig,
  SystemTaskTestResult,
} from "@/types/discussion";
import { PROVIDER_MODELS, VALID_PROVIDERS } from "./_providerModels";

function SystemTaskRow({ tcfg }: { tcfg: SystemTaskConfig }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [provider, setProvider] = useState(tcfg.effective_provider);
  const [model, setModel] = useState(tcfg.effective_model);
  const [testResult, setTestResult] = useState<SystemTaskTestResult | null>(null);
  const dirty =
    provider !== tcfg.effective_provider || model !== tcfg.effective_model;

  const modelOptions = (() => {
    const catalog = PROVIDER_MODELS[provider] ?? [];
    return catalog.includes(model) ? catalog : [model, ...catalog].filter(Boolean);
  })();

  function changeProvider(next: string) {
    setProvider(next);
    const firstModel = PROVIDER_MODELS[next]?.[0] ?? "";
    setModel(firstModel);
  }

  const save = useMutation({
    mutationFn: () => api.put(`/admin/system-tasks/${tcfg.task_id}`, { provider, model }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "system-tasks"] }),
  });

  const reset = useMutation({
    mutationFn: () => api.delete(`/admin/system-tasks/${tcfg.task_id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "system-tasks"] }),
  });

  const test = useMutation({
    mutationFn: () =>
      api.post<SystemTaskTestResult>(`/admin/system-tasks/${tcfg.task_id}/test`).then((r) => r.data),
    onSuccess: (data) => setTestResult(data),
    onError: (err: Error) => setTestResult({
      ok: false,
      provider,
      model,
      latency_ms: 0,
      sample_output: null,
      error: err.message ?? "request failed",
    }),
  });

  // Filter out claude_agent — system tasks don't use the agent SDK / tools.
  const taskProviders = VALID_PROVIDERS.filter((p) => p !== "claude_agent");

  // Format "last changed by foo@bar.com, 2h ago" — falls back to just
  // the email when the timestamp is too fresh / future-dated to format
  // sensibly.
  const auditLine = (() => {
    if (!tcfg.is_overridden || !tcfg.updated_at) return null;
    const at = new Date(tcfg.updated_at);
    const now = Date.now();
    const ms = Math.max(0, now - at.getTime());
    const min = Math.round(ms / 60_000);
    let rel: string;
    if (min < 1) rel = t("systemTasks.just_now");
    else if (min < 60) rel = t("systemTasks.minutes_ago", { count: min });
    else if (min < 60 * 24) rel = t("systemTasks.hours_ago", { count: Math.round(min / 60) });
    else rel = t("systemTasks.days_ago", { count: Math.round(min / 60 / 24) });
    return `${tcfg.updated_by_email ?? "system"} · ${rel}`;
  })();

  return (
    <div className="grid grid-cols-[200px_140px_1fr_auto] items-start gap-2 py-2 border-b border-border/40 text-xs">
      <div>
        <div className="font-medium text-sm">{tcfg.name}</div>
        <div className="text-[10px] text-muted-foreground leading-snug mt-0.5">
          {tcfg.description}
        </div>
        {tcfg.is_overridden && (
          <span className="inline-block mt-1 text-[9px] border border-warning/30 text-warning bg-warning/10 px-1 rounded">
            {t("personas.overridden")}
          </span>
        )}
        {auditLine && (
          <div className="text-[10px] text-muted-foreground mt-1">{auditLine}</div>
        )}
      </div>
      <select
        value={provider}
        onChange={(e) => changeProvider(e.target.value)}
        className="bg-background border border-border rounded px-2 py-1 text-xs"
      >
        {taskProviders.map((p) => <option key={p} value={p}>{p}</option>)}
      </select>
      <div className="flex flex-col gap-1">
        <select
          value={model}
          onChange={(e) => setModel(e.target.value)}
          className="bg-background border border-border rounded px-2 py-1 text-xs font-mono"
        >
          {modelOptions.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
        {testResult && (
          <div
            className={`text-[10px] rounded px-2 py-1 border ${
              testResult.ok
                ? "border-success/30 bg-success/10 text-success"
                : "border-danger/30 bg-danger/10 text-danger"
            }`}
          >
            {testResult.ok
              ? `✓ ${testResult.provider}/${testResult.model} · ${testResult.latency_ms}ms · ${(testResult.sample_output ?? "").slice(0, 40)}`
              : `✗ ${testResult.error ?? t("common.error")}`}
          </div>
        )}
      </div>
      <div className="flex gap-1">
        <button
          onClick={() => save.mutate()}
          disabled={!dirty || save.isPending}
          className="px-2.5 py-1 text-xs bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-30"
        >
          {save.isPending ? "…" : t("common.save")}
        </button>
        <button
          onClick={() => test.mutate()}
          disabled={test.isPending || dirty}
          title={dirty ? t("systemTasks.save_before_test") : t("systemTasks.test_hint")}
          className="px-2.5 py-1 text-xs border border-border text-muted-foreground rounded hover:text-foreground disabled:opacity-30"
        >
          {test.isPending ? "…" : t("systemTasks.test")}
        </button>
        {tcfg.is_overridden && (
          <button
            onClick={() => reset.mutate()}
            className="px-2.5 py-1 text-xs border border-border text-muted-foreground rounded hover:text-foreground"
          >
            {t("personas.reset")}
          </button>
        )}
      </div>
    </div>
  );
}

export function SystemTasksCard() {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible("admin.system-tasks");
  // staleTime + refetchInterval together so a second admin sees the
  // first admin's edits within 30 s without needing a manual refresh —
  // system_task_configs apply globally to the background scheduler, so
  // letting two admins drift on different versions is a real bug.
  const { data: list = [], isLoading } = useQuery<SystemTaskConfig[]>({
    queryKey: ["admin", "system-tasks"],
    queryFn: () => api.get("/admin/system-tasks").then((r) => r.data),
    staleTime: 30_000,
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-2">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title={t("systemTasks.title")}
        subtitle={t("systemTasks.subtitle")}
      />
      {open && (
        <>
          {isLoading ? (
            <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>
          ) : (
            <div className="grid grid-cols-[200px_140px_1fr_auto] gap-2 text-[10px] text-muted-foreground uppercase tracking-wider border-b border-border pb-1">
              <span>{t("systemTasks.task")}</span>
              <span>{t("personas.provider")}</span>
              <span>{t("personas.model")}</span>
              <span></span>
            </div>
          )}
          {list.map((tcfg) => <SystemTaskRow key={tcfg.task_id} tcfg={tcfg} />)}
        </>
      )}
    </div>
  );
}
