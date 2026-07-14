import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";

export interface LLMKeyInfo {
  provider: string;
  has_key: boolean;
  source: "db" | "env" | "none";
  masked: string;
  last_validated_at: string | null;
  last_validation_ok: boolean | null;
  last_validation_message: string | null;
  updated_at: string | null;
}

// Provider key-entry metadata. NOTE: this map deliberately lacks
// `claude_agent` — that "provider" is just a routing flag for the
// Claude Agent SDK (which itself uses ANTHROPIC_API_KEY). It appears in
// `providerColor` further down because personas can default to it, but
// there's no dedicated key to enter here. Same logic for `ollama` (no
// API key, runs locally).
const LLM_PROVIDER_LABELS: Record<string, { name: string; tagline: string; placeholder: string }> = {
  openai: {
    name: "ChatGPT (OpenAI)",
    tagline: "GPT-4o, GPT-4o-mini",
    placeholder: "sk-…",
  },
  anthropic: {
    name: "Claude (Anthropic)",
    tagline: "Claude Haiku / Sonnet / Opus",
    placeholder: "sk-ant-api…",
  },
  gemini: {
    name: "Gemini (Google)",
    tagline: "Gemini 2.0 Flash, 1.5 Pro",
    placeholder: "AIza…",
  },
  minimax: {
    name: "MiniMax",
    tagline: "MiniMax-M3, M2.7, abab6.5",
    placeholder: "eyJhbG…",
  },
  groq: {
    name: "Groq",
    tagline: "Llama 3.3 70B, Mixtral (super-fast)",
    placeholder: "gsk_…",
  },
  deepseek: {
    name: "DeepSeek",
    tagline: "deepseek-chat, deepseek-reasoner",
    placeholder: "sk-…",
  },
  openrouter: {
    name: "OpenRouter",
    tagline: "100+ models via one gateway",
    placeholder: "sk-or-…",
  },
};

function LLMKeyRow({ info }: { info: LLMKeyInfo }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const meta = LLM_PROVIDER_LABELS[info.provider] ?? {
    name: info.provider, tagline: "", placeholder: "API key",
  };
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => api.put(`/admin/llm-keys/${info.provider}`, { api_key: draft }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "llm-keys"] });
      setDraft("");
      setError(null);
    },
    onError: (err: Error) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? "save failed");
    },
  });

  const clear = useMutation({
    mutationFn: () => api.delete(`/admin/llm-keys/${info.provider}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "llm-keys"] }),
    onError: (err: Error) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? "clear failed");
    },
  });

  const test = useMutation<{ data: { ok: boolean; message: string } }>({
    mutationFn: () => api.post(`/admin/llm-keys/${info.provider}/test`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "llm-keys"] }),
    onError: (err: Error) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? "test failed");
    },
  });

  const sourceBadge =
    info.source === "db" ? "DB" :
    info.source === "env" ? ".env" :
    "—";
  const sourceColor =
    info.source === "db" ? "text-success bg-success/10 border-success/30" :
    info.source === "env" ? "text-info bg-info/10 border-info/30" :
    "text-muted-foreground bg-muted/10 border-border";

  const lastValidationBadge = info.last_validation_ok === true
    ? <span className="text-xs text-success">✓ {t("llm_keys.validated")}</span>
    : info.last_validation_ok === false
    ? <span className="text-xs text-danger" title={info.last_validation_message ?? ""}>
        ✗ {info.last_validation_message ? info.last_validation_message.slice(0, 60) : t("llm_keys.invalid")}
      </span>
    : null;

  const testResult = test.data?.data;

  return (
    <div className="border border-border rounded-lg p-3 space-y-2">
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <div>
          <span className="font-medium text-sm">{meta.name}</span>
          <span className="text-xs text-muted-foreground ml-2">{meta.tagline}</span>
        </div>
        <span className={`text-micro border px-1.5 py-0.5 rounded ${sourceColor}`}>{sourceBadge}</span>
      </div>

      {info.has_key && (
        <p className="text-xs text-muted-foreground font-mono">
          {info.masked || "(set)"}
        </p>
      )}

      <div className="flex gap-2 items-center flex-wrap">
        <input
          type="password"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={info.has_key ? t("llm_keys.replace_placeholder") : meta.placeholder}
          className="flex-1 min-w-[200px] bg-background border border-border rounded px-2 py-1.5 text-xs font-mono text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:border-primary/50"
        />
        <button
          onClick={() => draft.trim() && save.mutate()}
          disabled={!draft.trim() || save.isPending}
          className="px-3 py-1.5 text-xs bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-40"
        >
          {save.isPending ? t("common.saving") : t("common.save")}
        </button>
        {info.has_key && info.source === "db" && (
          <button
            onClick={() => clear.mutate()}
            disabled={clear.isPending}
            className="px-3 py-1.5 text-xs border border-border text-muted-foreground rounded hover:text-danger disabled:opacity-40"
          >
            {t("llm_keys.clear")}
          </button>
        )}
        <button
          onClick={() => test.mutate()}
          disabled={!info.has_key || test.isPending}
          className="px-3 py-1.5 text-xs border border-border text-muted-foreground rounded hover:text-foreground disabled:opacity-40"
        >
          {test.isPending ? t("llm_keys.testing") : t("llm_keys.test")}
        </button>
      </div>

      <div className="flex items-center gap-3 min-h-[1.25rem]">
        {error && <span className="text-xs text-danger">{error}</span>}
        {testResult && (
          <span className={`text-xs ${testResult.ok ? "text-success" : "text-danger"}`}>
            {testResult.ok ? `✓ ${t("llm_keys.validated")}` : `✗ ${testResult.message.slice(0, 80)}`}
          </span>
        )}
        {!error && !testResult && lastValidationBadge}
      </div>
    </div>
  );
}

export function LLMKeysCard() {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible("admin.llm-keys");
  const { data: keys = [], isLoading } = useQuery<LLMKeyInfo[]>({
    queryKey: ["admin", "llm-keys"],
    queryFn: () => api.get("/admin/llm-keys").then((r) => r.data),
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title={t("llm_keys.title")}
        subtitle={t("llm_keys.subtitle")}
      />
      {open && (
        isLoading ? (
          <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>
        ) : (
          <div className="space-y-2">
            {keys.map((k) => <LLMKeyRow key={k.provider} info={k} />)}
          </div>
        )
      )}
    </div>
  );
}
