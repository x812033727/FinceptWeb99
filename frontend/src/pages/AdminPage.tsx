import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAuthStore } from "@/store/authStore";
import { useCheckForUpdates, useTriggerUpdate, useVersion } from "@/hooks/useVersion";
import api from "@/lib/api";

interface AdminUser {
  id: string;
  email: string;
  role: "viewer" | "analyst" | "admin";
  is_active: boolean;
  created_at: string;
}

interface SystemStats {
  total_users: number;
  active_users: number;
  users_by_role: Record<string, number>;
  total_alerts: number;
  total_watchlists: number;
}

const ROLE_COLORS: Record<string, string> = {
  admin:   "text-amber-400 bg-amber-400/10 border-amber-400/30",
  analyst: "text-blue-400 bg-blue-400/10 border-blue-400/30",
  viewer:  "text-muted-foreground bg-muted/20 border-border",
};

function SystemUpdateCard() {
  const { data: version, isLoading } = useVersion();
  const check = useCheckForUpdates();
  const trigger = useTriggerUpdate();

  const status = trigger.data?.status;
  const message = trigger.data?.message;
  const checkError = check.isError;

  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <p className="text-sm font-medium">System update</p>
          {isLoading || !version ? (
            <p className="text-xs text-muted-foreground animate-pulse mt-1">
              Checking GitHub…
            </p>
          ) : (
            <p className="text-xs text-muted-foreground mt-1">
              Current <span className="font-mono">v{version.current}</span>
              {" · "}
              Latest <span className="font-mono">v{version.latest}</span>
              {version.update_available && (
                <span className="ml-2 text-amber-500">update available</span>
              )}
            </p>
          )}
          {checkError && (
            <p className="text-xs text-red-500 mt-2">Failed to reach GitHub. Try again.</p>
          )}
          {status && (
            <p
              className={`text-xs mt-2 ${
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
    </div>
  );
}

// ── LLM provider keys ─────────────────────────────────────────────

interface LLMKeyInfo {
  provider: string;
  has_key: boolean;
  source: "db" | "env" | "none";
  masked: string;
  last_validated_at: string | null;
  last_validation_ok: boolean | null;
  last_validation_message: string | null;
  updated_at: string | null;
}

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
    tagline: "MiniMax-M2.7, abab6.5",
    placeholder: "eyJhbG…",
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
    info.source === "db" ? "text-green-400 bg-green-400/10 border-green-400/30" :
    info.source === "env" ? "text-blue-400 bg-blue-400/10 border-blue-400/30" :
    "text-muted-foreground bg-muted/10 border-border";

  const lastValidationBadge = info.last_validation_ok === true
    ? <span className="text-xs text-green-400">✓ {t("llm_keys.validated")}</span>
    : info.last_validation_ok === false
    ? <span className="text-xs text-red-400" title={info.last_validation_message ?? ""}>
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
        <span className={`text-[10px] border px-1.5 py-0.5 rounded ${sourceColor}`}>{sourceBadge}</span>
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
            className="px-3 py-1.5 text-xs border border-border text-muted-foreground rounded hover:text-red-400 disabled:opacity-40"
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
        {error && <span className="text-xs text-red-400">{error}</span>}
        {testResult && (
          <span className={`text-xs ${testResult.ok ? "text-green-400" : "text-red-400"}`}>
            {testResult.ok ? `✓ ${t("llm_keys.validated")}` : `✗ ${testResult.message.slice(0, 80)}`}
          </span>
        )}
        {!error && !testResult && lastValidationBadge}
      </div>
    </div>
  );
}

// ── Persona model routing ────────────────────────────────────────

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

// Curated model catalog per provider — keeps the persona-routing UI a pure
// dropdown so admins don't have to memorise model strings. Update as
// providers ship new models. If a persona is currently set to a model not
// in this list (legacy override or a brand-new model), the row prepends it
// to the dropdown so the value remains selectable.
const PROVIDER_MODELS: Record<string, string[]> = {
  openai:    ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo", "o1", "o1-mini", "o3-mini"],
  anthropic: ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-sonnet-4-5-20250929", "claude-opus-4-7"],
  gemini:    ["gemini-2.0-flash", "gemini-2.0-flash-exp", "gemini-1.5-pro", "gemini-1.5-flash", "gemini-1.5-flash-8b"],
  // Ollama models depend on what the operator has `ollama pull`-ed locally;
  // these are popular community defaults.
  ollama:    ["llama3.2", "llama3.3:70b", "qwen2.5:14b", "qwen2.5:72b", "mistral-nemo", "deepseek-r1:32b", "phi3"],
  minimax:   ["MiniMax-M2.7", "MiniMax-M2.7-highspeed", "abab6.5s-chat", "abab6.5-chat", "MiniMax-Text-01"],
  groq:      ["llama-3.3-70b-versatile", "llama-3.1-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
  deepseek:  ["deepseek-chat", "deepseek-reasoner"],
  // OpenRouter has 100+ models; pick a curated set covering the major
  // model families. Power users editing a route to something exotic still
  // see their custom value (prepended) — they just can't browse the long
  // tail from this UI.
  openrouter: [
    "openai/gpt-4o", "openai/gpt-4o-mini",
    "anthropic/claude-3.5-sonnet", "anthropic/claude-3-opus",
    "google/gemini-pro-1.5", "google/gemini-flash-1.5",
    "meta-llama/llama-3.1-70b-instruct", "mistralai/mixtral-8x22b-instruct",
    "deepseek/deepseek-chat", "qwen/qwen-2.5-72b-instruct",
  ],
  // claude_agent uses the Claude Agent SDK which under the hood talks to
  // anthropic's Claude models — same catalog.
  claude_agent: ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001", "claude-opus-4-7", "claude-sonnet-4-6"],
};

const VALID_PROVIDERS = Object.keys(PROVIDER_MODELS);

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

function PersonasCard() {
  const { t } = useTranslation();
  const { data: list = [], isLoading } = useQuery<PersonaConfig[]>({
    queryKey: ["admin", "personas"],
    queryFn: () => api.get("/admin/personas").then((r) => r.data),
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-2">
      <div>
        <p className="text-sm font-medium">{t("personas.title")}</p>
        <p className="text-xs text-muted-foreground mt-0.5">{t("personas.subtitle")}</p>
      </div>
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
    </div>
  );
}


// ── LLM usage summary ───────────────────────────────────────────

interface UsageBucket {
  provider: string;
  model: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
}

interface UsageDay {
  date: string;
  cost_usd: number;
  requests: number;
}

interface UsageSummary {
  range_days: number;
  user_scoped: boolean;
  total_requests: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_cost_usd: number;
  by_provider: UsageBucket[];
  by_day: UsageDay[];
}

const PROVIDER_COLOR: Record<string, string> = {
  openai: "text-green-400",
  anthropic: "text-orange-400",
  gemini: "text-blue-400",
  minimax: "text-purple-400",
  groq: "text-pink-400",
  deepseek: "text-cyan-400",
  openrouter: "text-yellow-400",
  ollama: "text-violet-400",
};

export function UsageCard({ scope }: { scope: "admin" | "me" }) {
  const { t } = useTranslation();
  const [days, setDays] = useState(30);
  const path = scope === "admin" ? "/admin/llm-usage" : "/auth/llm-usage";
  const { data, isLoading } = useQuery<UsageSummary>({
    queryKey: ["llm-usage", scope, days],
    queryFn: () => api.get(`${path}?range_days=${days}`).then((r) => r.data),
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium">{t("usage.title")}</p>
          <p className="text-xs text-muted-foreground mt-0.5">
            {scope === "admin" ? t("usage.subtitle_admin") : t("usage.subtitle_me")}
          </p>
        </div>
        <div className="flex gap-1">
          {[7, 30, 90].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`px-2.5 py-1 text-xs rounded ${
                days === d
                  ? "bg-primary text-primary-foreground"
                  : "border border-border text-muted-foreground hover:text-foreground"
              }`}
            >
              {d}d
            </button>
          ))}
        </div>
      </div>

      {isLoading || !data ? (
        <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>
      ) : data.total_requests === 0 ? (
        <p className="text-xs text-muted-foreground py-3">{t("usage.empty")}</p>
      ) : (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <Stat label={t("usage.cost")} value={`$${data.total_cost_usd.toFixed(4)}`} />
            <Stat label={t("usage.requests")} value={data.total_requests.toLocaleString()} />
            <Stat label={t("usage.prompt_tokens")} value={data.total_prompt_tokens.toLocaleString()} />
            <Stat label={t("usage.completion_tokens")} value={data.total_completion_tokens.toLocaleString()} />
          </div>

          <div className="space-y-1">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">{t("usage.by_provider")}</p>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted-foreground border-b border-border/50">
                  <th className="text-left py-1 font-medium">{t("personas.provider")}</th>
                  <th className="text-left py-1 font-medium">{t("personas.model")}</th>
                  <th className="text-right py-1 font-medium">{t("usage.requests")}</th>
                  <th className="text-right py-1 font-medium">tokens</th>
                  <th className="text-right py-1 font-medium">{t("usage.cost")}</th>
                </tr>
              </thead>
              <tbody>
                {data.by_provider.map((b, i) => (
                  <tr key={i} className="border-b border-border/30">
                    <td className={`py-1 font-medium ${PROVIDER_COLOR[b.provider] ?? "text-foreground"}`}>{b.provider}</td>
                    <td className="py-1 font-mono text-muted-foreground">{b.model}</td>
                    <td className="py-1 text-right">{b.requests.toLocaleString()}</td>
                    <td className="py-1 text-right text-muted-foreground">
                      {(b.prompt_tokens + b.completion_tokens).toLocaleString()}
                    </td>
                    <td className="py-1 text-right font-medium">${b.cost_usd.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-secondary/30 rounded p-2">
      <p className="text-[10px] text-muted-foreground uppercase tracking-wider">{label}</p>
      <p className="text-sm font-semibold mt-0.5 tabular-nums">{value}</p>
    </div>
  );
}


function LLMKeysCard() {
  const { t } = useTranslation();
  const { data: keys = [], isLoading } = useQuery<LLMKeyInfo[]>({
    queryKey: ["admin", "llm-keys"],
    queryFn: () => api.get("/admin/llm-keys").then((r) => r.data),
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <div>
        <p className="text-sm font-medium">{t("llm_keys.title")}</p>
        <p className="text-xs text-muted-foreground mt-0.5">{t("llm_keys.subtitle")}</p>
      </div>
      {isLoading ? (
        <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>
      ) : (
        <div className="space-y-2">
          {keys.map((k) => <LLMKeyRow key={k.provider} info={k} />)}
        </div>
      )}
    </div>
  );
}


// ── Market data provider keys ────────────────────────────────────

interface MarketKeyInfo {
  provider: string;
  has_key: boolean;
  source: "db" | "env" | "none";
  masked: string;
  last_validated_at: string | null;
  last_validation_ok: boolean | null;
  last_validation_message: string | null;
  updated_at: string | null;
}

const MARKET_PROVIDER_LABELS: Record<string, { name: string; tagline: string; placeholder: string; signupUrl: string }> = {
  finnhub: {
    name: "Finnhub",
    tagline: "US 4th-tier quote fallback · 60 req/min free",
    placeholder: "c…",
    signupUrl: "https://finnhub.io/dashboard",
  },
};

function MarketKeyRow({ info }: { info: MarketKeyInfo }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const meta = MARKET_PROVIDER_LABELS[info.provider] ?? {
    name: info.provider, tagline: "", placeholder: "API key", signupUrl: "",
  };
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => api.put(`/admin/market-keys/${info.provider}`, { api_key: draft }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "market-keys"] });
      setDraft("");
      setError(null);
    },
    onError: (err: Error) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? "save failed");
    },
  });

  const clear = useMutation({
    mutationFn: () => api.delete(`/admin/market-keys/${info.provider}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "market-keys"] }),
    onError: (err: Error) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? "clear failed");
    },
  });

  const test = useMutation<{ data: { ok: boolean; message: string } }>({
    mutationFn: () => api.post(`/admin/market-keys/${info.provider}/test`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "market-keys"] }),
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
    info.source === "db" ? "text-green-400 bg-green-400/10 border-green-400/30" :
    info.source === "env" ? "text-blue-400 bg-blue-400/10 border-blue-400/30" :
    "text-muted-foreground bg-muted/10 border-border";

  const lastValidationBadge = info.last_validation_ok === true
    ? <span className="text-xs text-green-400">✓ {t("llm_keys.validated")}</span>
    : info.last_validation_ok === false
    ? <span className="text-xs text-red-400" title={info.last_validation_message ?? ""}>
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
          {meta.signupUrl && !info.has_key && (
            <a
              href={meta.signupUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-primary hover:underline ml-2"
            >
              {t("market_keys.get_key")} →
            </a>
          )}
        </div>
        <span className={`text-[10px] border px-1.5 py-0.5 rounded ${sourceColor}`}>{sourceBadge}</span>
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
            className="px-3 py-1.5 text-xs border border-border text-muted-foreground rounded hover:text-red-400 disabled:opacity-40"
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
        {error && <span className="text-xs text-red-400">{error}</span>}
        {testResult && (
          <span className={`text-xs ${testResult.ok ? "text-green-400" : "text-red-400"}`}>
            {testResult.ok ? `✓ ${t("llm_keys.validated")}` : `✗ ${testResult.message.slice(0, 80)}`}
          </span>
        )}
        {!error && !testResult && lastValidationBadge}
      </div>
    </div>
  );
}


function MarketKeysCard() {
  const { t } = useTranslation();
  const { data: keys = [], isLoading } = useQuery<MarketKeyInfo[]>({
    queryKey: ["admin", "market-keys"],
    queryFn: () => api.get("/admin/market-keys").then((r) => r.data),
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <div>
        <p className="text-sm font-medium">{t("market_keys.title")}</p>
        <p className="text-xs text-muted-foreground mt-0.5">{t("market_keys.subtitle")}</p>
      </div>
      {isLoading ? (
        <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>
      ) : (
        <div className="space-y-2">
          {keys.map((k) => <MarketKeyRow key={k.provider} info={k} />)}
        </div>
      )}
    </div>
  );
}


interface IngestHealth {
  job_id: string;
  last_run_at: string | null;
  ok: boolean;
  row_count: number;
  error: string | null;
}

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "—";
  const diffSec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.round(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.round(diffSec / 3600)}h ago`;
  return `${Math.round(diffSec / 86400)}d ago`;
}

function IngestHealthCard() {
  const { data: rows = [], isLoading } = useQuery<IngestHealth[]>({
    queryKey: ["admin", "ingest-health"],
    queryFn: () => api.get("/admin/ingest/health").then((r) => r.data),
    refetchInterval: 60_000,
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-medium">Scheduled Ingest Health</h2>
        <span className="text-[10px] text-muted-foreground">
          refreshes every 60s
        </span>
      </div>

      {isLoading ? (
        <p className="text-xs text-muted-foreground animate-pulse">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No ingest jobs have reported yet — first cron tick is pending.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-[10px] text-muted-foreground uppercase tracking-wider">
                <th className="text-left py-1.5 pr-3">Job</th>
                <th className="text-left py-1.5 pr-3">Status</th>
                <th className="text-right py-1.5 pr-3">Rows</th>
                <th className="text-left py-1.5 pr-3">Last Run</th>
                <th className="text-left py-1.5">Error</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {rows.map((r) => (
                <tr key={r.job_id}>
                  <td className="py-1.5 pr-3 font-mono">{r.job_id}</td>
                  <td className="py-1.5 pr-3">
                    <span
                      className={`px-1.5 py-0.5 rounded text-[10px] ${
                        r.ok
                          ? "bg-green-500/10 text-green-400 border border-green-500/30"
                          : "bg-red-500/10 text-red-400 border border-red-500/30"
                      }`}
                    >
                      {r.ok ? "ok" : "error"}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">
                    {r.row_count.toLocaleString()}
                  </td>
                  <td className="py-1.5 pr-3 text-muted-foreground">
                    {timeAgo(r.last_run_at)}
                  </td>
                  <td
                    className="py-1.5 text-muted-foreground truncate max-w-[24rem]"
                    title={r.error ?? undefined}
                  >
                    {r.error ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}


export default function AdminPage() {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();

  if (user?.role !== "admin") {
    navigate("/dashboard", { replace: true });
    return null;
  }

  return <AdminContent />;
}

function AdminContent() {
  const qc = useQueryClient();

  const { data: stats } = useQuery<SystemStats>({
    queryKey: ["admin", "stats"],
    queryFn: () => api.get("/admin/stats").then((r) => r.data),
  });

  const { data: users = [], isLoading } = useQuery<AdminUser[]>({
    queryKey: ["admin", "users"],
    queryFn: () => api.get("/admin/users?limit=200").then((r) => r.data),
  });

  const [editingId, setEditingId] = useState<string | null>(null);

  const updateRole = useMutation({
    mutationFn: ({ id, role }: { id: string; role: string }) =>
      api.patch(`/admin/users/${id}/role`, { role }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin"] });
      setEditingId(null);
    },
  });

  const toggleActive = useMutation({
    mutationFn: ({ id, is_active }: { id: string; is_active: boolean }) =>
      api.patch(`/admin/users/${id}/active`, { is_active }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin"] }),
  });

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <h1 className="text-xl font-semibold">Admin</h1>

      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {[
            { label: "Total Users", value: stats.total_users },
            { label: "Active", value: stats.active_users },
            { label: "Viewers", value: stats.users_by_role.viewer ?? 0 },
            { label: "Alerts", value: stats.total_alerts },
            { label: "Watchlists", value: stats.total_watchlists },
          ].map(({ label, value }) => (
            <div
              key={label}
              className="bg-card border border-border rounded-lg p-3 text-center"
            >
              <p className="text-2xl font-bold tabular-nums">{value}</p>
              <p className="text-xs text-muted-foreground mt-0.5">{label}</p>
            </div>
          ))}
        </div>
      )}

      <SystemUpdateCard />

      <LLMKeysCard />

      <MarketKeysCard />

      <UsageCard scope="admin" />

      <IngestHealthCard />

      <PersonasCard />

      {/* User table */}
      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-border flex items-center justify-between">
          <span className="text-sm font-medium">Users ({users.length})</span>
        </div>

        {isLoading ? (
          <p className="p-4 text-xs text-muted-foreground animate-pulse">Loading…</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  {["Email", "Role", "Status", "Joined", "Actions"].map((h) => (
                    <th
                      key={h}
                      className="px-4 py-2.5 text-left text-xs font-medium text-muted-foreground"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {users.map((u) => (
                  <tr key={u.id} className="hover:bg-accent/5">
                    <td className="px-4 py-2.5 text-xs">{u.email}</td>
                    <td className="px-4 py-2.5">
                      {editingId === u.id ? (
                        <select
                          defaultValue={u.role}
                          className="bg-background border border-border rounded px-1.5 py-0.5 text-xs"
                          onChange={(e) =>
                            updateRole.mutate({ id: u.id, role: e.target.value })
                          }
                          onBlur={() => setEditingId(null)}
                          autoFocus
                        >
                          <option value="viewer">viewer</option>
                          <option value="analyst">analyst</option>
                          <option value="admin">admin</option>
                        </select>
                      ) : (
                        <button
                          onClick={() => setEditingId(u.id)}
                          className={`text-xs border rounded px-1.5 py-0.5 ${ROLE_COLORS[u.role]}`}
                          title="Click to change role"
                        >
                          {u.role}
                        </button>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`text-xs ${
                          u.is_active ? "text-green-400" : "text-red-400"
                        }`}
                      >
                        {u.is_active ? "active" : "disabled"}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-xs text-muted-foreground">
                      {new Date(u.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-2.5">
                      <button
                        onClick={() =>
                          toggleActive.mutate({ id: u.id, is_active: !u.is_active })
                        }
                        className="text-xs text-muted-foreground hover:text-foreground transition-colors"
                      >
                        {u.is_active ? "Disable" : "Enable"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
