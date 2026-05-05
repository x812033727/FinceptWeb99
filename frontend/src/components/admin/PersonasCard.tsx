import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import { PROVIDER_MODELS, VALID_PROVIDERS } from "./_providerModels";

interface PersonaConfig {
  persona_id: string;
  name: string;
  description: string;
  default_provider: string;
  default_model: string;
  effective_provider: string;
  effective_model: string;
  is_overridden: boolean;
}

function PersonaRow({ p }: { p: PersonaConfig }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [provider, setProvider] = useState(p.effective_provider);
  const [model, setModel] = useState(p.effective_model);
  const dirty = provider !== p.effective_provider || model !== p.effective_model;

  // Build the model option list. If the current value isn't in the catalog
  // (legacy / custom override), prepend it so the <select> can display it
  // as the chosen option without silently dropping it.
  const modelOptions = (() => {
    const catalog = PROVIDER_MODELS[provider] ?? [];
    return catalog.includes(model) ? catalog : [model, ...catalog].filter(Boolean);
  })();

  function changeProvider(next: string) {
    setProvider(next);
    // Reset model to the new provider's first known option — keeps the
    // selection valid; otherwise a user switching openai → anthropic would
    // be left with a model id that anthropic rejects.
    const firstModel = PROVIDER_MODELS[next]?.[0] ?? "";
    setModel(firstModel);
  }

  const save = useMutation({
    mutationFn: () => api.put(`/admin/personas/${p.persona_id}`, { provider, model }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "personas"] }),
  });

  const reset = useMutation({
    mutationFn: () => api.delete(`/admin/personas/${p.persona_id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "personas"] }),
  });

  return (
    <div className="grid grid-cols-[200px_140px_1fr_auto] items-center gap-2 py-1.5 border-b border-border/40 text-xs">
      <div>
        <span className="font-medium text-sm">{p.name}</span>
        {p.is_overridden && (
          <span className="ml-1.5 text-[9px] border border-amber-400/30 text-amber-400 bg-amber-400/10 px-1 rounded">
            {t("personas.overridden")}
          </span>
        )}
      </div>
      <select
        value={provider}
        onChange={(e) => changeProvider(e.target.value)}
        className="bg-background border border-border rounded px-2 py-1 text-xs"
      >
        {VALID_PROVIDERS.map((p) => <option key={p} value={p}>{p}</option>)}
      </select>
      <select
        value={model}
        onChange={(e) => setModel(e.target.value)}
        className="bg-background border border-border rounded px-2 py-1 text-xs font-mono"
      >
        {modelOptions.map((m) => <option key={m} value={m}>{m}</option>)}
      </select>
      <div className="flex gap-1">
        <button
          onClick={() => save.mutate()}
          disabled={!dirty || save.isPending}
          className="px-2.5 py-1 text-xs bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-30"
        >
          {save.isPending ? "…" : t("common.save")}
        </button>
        {p.is_overridden && (
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

export function PersonasCard() {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible("admin.personas");
  const { data: list = [], isLoading } = useQuery<PersonaConfig[]>({
    queryKey: ["admin", "personas"],
    queryFn: () => api.get("/admin/personas").then((r) => r.data),
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-2">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title={t("personas.title")}
        subtitle={t("personas.subtitle")}
      />
      {open && (
        <>
          {isLoading ? (
            <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>
          ) : (
            <div className="grid grid-cols-[200px_140px_1fr_auto] gap-2 text-[10px] text-muted-foreground uppercase tracking-wider border-b border-border pb-1">
              <span>{t("personas.persona")}</span>
              <span>{t("personas.provider")}</span>
              <span>{t("personas.model")}</span>
              <span></span>
            </div>
          )}
          {list.map((p) => <PersonaRow key={p.persona_id} p={p} />)}
        </>
      )}
    </div>
  );
}
