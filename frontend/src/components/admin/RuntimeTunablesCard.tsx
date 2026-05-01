import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import { CollapsibleHeader, useCollapsible } from "@/components/Collapsible";

interface RuntimeSetting {
  key: string;
  type: "int" | "float" | "bool" | "str";
  name: string;
  description: string;
  min_value: number | null;
  max_value: number | null;
  default_value: unknown;
  effective_value: unknown;
  is_overridden: boolean;
  updated_at: string | null;
  updated_by_email: string | null;
}

function RuntimeSettingRow({ s }: { s: RuntimeSetting }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [draft, setDraft] = useState(String(s.effective_value));
  const dirty = draft !== String(s.effective_value);

  const save = useMutation({
    mutationFn: () => {
      // Cast back to the spec's declared type before sending — the
      // backend re-validates anyway, but typed JSON keeps the API tidy.
      let value: unknown = draft;
      if (s.type === "int") value = parseInt(draft, 10);
      else if (s.type === "float") value = parseFloat(draft);
      else if (s.type === "bool") value = draft === "true" || draft === "1";
      return api.put(`/admin/runtime-settings/${s.key}`, { value });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "runtime-settings"] }),
  });
  const reset = useMutation({
    mutationFn: () => api.delete(`/admin/runtime-settings/${s.key}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "runtime-settings"] }),
  });

  const auditLine = (() => {
    if (!s.is_overridden || !s.updated_at) return null;
    const at = new Date(s.updated_at);
    // eslint-disable-next-line react-hooks/purity
    const now = Date.now();
    const min = Math.round(Math.max(0, now - at.getTime()) / 60_000);
    let rel: string;
    if (min < 1) rel = t("systemTasks.just_now");
    else if (min < 60) rel = t("systemTasks.minutes_ago", { count: min });
    else if (min < 60 * 24) rel = t("systemTasks.hours_ago", { count: Math.round(min / 60) });
    else rel = t("systemTasks.days_ago", { count: Math.round(min / 60 / 24) });
    return `${s.updated_by_email ?? "system"} · ${rel}`;
  })();

  // The min/max hints are advisory; backend re-validates.
  const rangeHint = (() => {
    if (s.type !== "int" && s.type !== "float") return null;
    if (s.min_value === null && s.max_value === null) return null;
    return `${s.min_value ?? "−∞"} – ${s.max_value ?? "∞"}`;
  })();

  return (
    <div className="grid grid-cols-[260px_1fr_120px_auto] items-start gap-2 py-2 border-b border-border/40 text-xs">
      <div>
        <div className="font-medium text-sm">{s.name}</div>
        <div className="text-[10px] text-muted-foreground font-mono mt-0.5">{s.key}</div>
        {s.is_overridden && (
          <span className="inline-block mt-1 text-[9px] border border-amber-400/30 text-amber-400 bg-amber-400/10 px-1 rounded">
            {t("personas.overridden")}
          </span>
        )}
        {auditLine && (
          <div className="text-[10px] text-muted-foreground mt-1">{auditLine}</div>
        )}
      </div>
      <div className="text-[11px] text-muted-foreground leading-snug">
        {s.description}
        {rangeHint && (
          <span className="ml-1 text-[10px] font-mono">[{rangeHint}]</span>
        )}
        <div className="text-[10px] mt-1">
          {t("runtimeTunables.default")}：
          <span className="font-mono ml-1">{String(s.default_value)}</span>
        </div>
      </div>
      <div>
        {s.type === "bool" ? (
          <select
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="bg-background border border-border rounded px-2 py-1 text-xs w-full"
          >
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
        ) : (
          <input
            type={s.type === "int" || s.type === "float" ? "number" : "text"}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            min={s.min_value ?? undefined}
            max={s.max_value ?? undefined}
            step={s.type === "int" ? 1 : "any"}
            className="bg-background border border-border rounded px-2 py-1 text-xs w-full font-mono"
          />
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
        {s.is_overridden && (
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

export function RuntimeTunablesCard() {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible("admin.runtime-tunables");
  const { data: list = [], isLoading } = useQuery<RuntimeSetting[]>({
    queryKey: ["admin", "runtime-settings"],
    queryFn: () => api.get("/admin/runtime-settings").then((r) => r.data),
    staleTime: 30_000,
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-2">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title={t("runtimeTunables.title")}
        subtitle={t("runtimeTunables.subtitle")}
      />
      {open && (
        <>
          {isLoading ? (
            <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>
          ) : (
            <div className="grid grid-cols-[260px_1fr_120px_auto] gap-2 text-[10px] text-muted-foreground uppercase tracking-wider border-b border-border pb-1">
              <span>{t("runtimeTunables.setting")}</span>
              <span>{t("runtimeTunables.description")}</span>
              <span>{t("runtimeTunables.value")}</span>
              <span></span>
            </div>
          )}
          {list.map((s) => <RuntimeSettingRow key={s.key} s={s} />)}
        </>
      )}
    </div>
  );
}
