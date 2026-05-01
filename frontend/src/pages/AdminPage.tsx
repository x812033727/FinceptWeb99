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

// Provider key-entry metadata. NOTE: this map deliberately lacks
// `claude_agent` — that "provider" is just a routing flag for the
// Claude Agent SDK (which itself uses ANTHROPIC_API_KEY). It appears in
// `providerColor` further down because personas can default to it, but
// there's no dedicated key to enter in `LLMKeysCard`. Same logic for
// `ollama` (no API key, runs locally).
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

// ── Background system tasks (LLM routing) ────────────────────────

interface SystemTaskConfig {
  task_id: string;
  name: string;
  description: string;
  default_provider: string;
  default_model: string;
  effective_provider: string;
  effective_model: string;
  is_overridden: boolean;
  updated_at: string | null;
  updated_by_email: string | null;
}

interface SystemTaskTestResult {
  ok: boolean;
  provider: string;
  model: string;
  latency_ms: number;
  sample_output: string | null;
  error: string | null;
}

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
          <span className="inline-block mt-1 text-[9px] border border-amber-400/30 text-amber-400 bg-amber-400/10 px-1 rounded">
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
                ? "border-green-700/50 bg-green-900/20 text-green-300"
                : "border-red-700/50 bg-red-900/20 text-red-300"
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

function SystemTasksCard() {
  const { t } = useTranslation();
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
      <div>
        <p className="text-sm font-medium">{t("systemTasks.title")}</p>
        <p className="text-xs text-muted-foreground mt-0.5">{t("systemTasks.subtitle")}</p>
      </div>
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
    </div>
  );
}


// ── Runtime tunables (env-var-equivalent settings tunable by admin) ─

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

function RuntimeTunablesCard() {
  const { t } = useTranslation();
  const { data: list = [], isLoading } = useQuery<RuntimeSetting[]>({
    queryKey: ["admin", "runtime-settings"],
    queryFn: () => api.get("/admin/runtime-settings").then((r) => r.data),
    staleTime: 30_000,
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-2">
      <div>
        <p className="text-sm font-medium">{t("runtimeTunables.title")}</p>
        <p className="text-xs text-muted-foreground mt-0.5">{t("runtimeTunables.subtitle")}</p>
      </div>
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
  finmind: {
    name: "FinMind",
    tagline: "TW news + institutional + monthly revenue · 600 req/day free",
    placeholder: "eyJhbGc…",
    signupUrl: "https://finmindtrade.com/",
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

interface IngestRetryResult {
  status: string;
  message: string;
}

// Mirror of `RETRYABLE_INGEST_JOBS` in `backend/api/admin/router.py`.
// Adding a job here without registering it backend-side just makes the
// button render — the POST will 404. Keep the two sides in sync.
const RETRYABLE_INGEST_JOBS = new Set([
  "ingest_news_tw",
  "ingest_news_international",
  "ingest_institutional_tw",
  "ingest_margin_tw",
  "ingest_revenue_tw",
  "ingest_taiex_history",
  "score_discussion_outcomes",
]);

// Per-job metadata for the IngestHealthCard. `schedule_zh` is mirrored
// from the corresponding `add_job` call in `backend/tasks/scheduler.py`
// — when you change the cron expression there, update the entry here
// or the displayed schedule will silently drift out of sync.
// `description_zh` is a 1-line summary in 繁體中文 so the table is
// readable for non-engineering operators.
interface JobMeta {
  description_zh: string;
  schedule_zh: string;
}
const JOB_META: Record<string, JobMeta> = {
  ingest_news_tw: {
    description_zh: "台股新聞抓取（Google News RSS）",
    schedule_zh: "每 1 小時",
  },
  ingest_news_international: {
    description_zh: "國際財經新聞抓取（Fed / 美股 / 國際）",
    schedule_zh: "每 1 小時",
  },
  ingest_ohlcv_tw: {
    description_zh: "台股每日 K 線（TWSE → FinMind 後備）",
    schedule_zh: "每天 14:30 (台北)",
  },
  ingest_fundamentals_tw: {
    description_zh: "台股基本面（PE / PB / 殖利率）",
    schedule_zh: "每天 14:45 (台北)",
  },
  ingest_institutional_tw: {
    description_zh: "台股法人買賣超（外資 / 投信 / 自營商）",
    schedule_zh: "每天 14:50 (台北)",
  },
  ingest_margin_tw: {
    description_zh: "台股融資融券餘額",
    schedule_zh: "每天 15:00 (台北)",
  },
  ingest_taiex_history: {
    description_zh: "TAIEX 大盤指數每日歷史線",
    schedule_zh: "每天 15:10 (台北)",
  },
  ingest_revenue_tw: {
    description_zh: "台股月營收（FinMind 全市場一次抓）",
    schedule_zh: "每天 17:00 (台北)",
  },
  ingest_quotes_retention_tw: {
    description_zh: "台股 quote_snapshots 30 日保留（清舊資料）",
    schedule_zh: "每天 11:00 (台北)",
  },
  score_news_sentiment: {
    description_zh: "新聞情緒評分（LLM 評每篇利多 / 利空 / 中性）",
    schedule_zh: "每 30 分鐘",
  },
  auto_run_discussion: {
    description_zh: "每日自動圓桌討論（已啟用 opt-in 的使用者）",
    schedule_zh: "每天 08:00 (台北)",
  },
  verify_discussion_outcome: {
    description_zh: "圓桌討論勝負判定（max-high vs day1_open × 1.03）",
    schedule_zh: "每天 16:30 (台北)",
  },
  score_discussion_outcomes: {
    description_zh: "圓桌討論「對答案」D1-D5 收盤漲跌計算",
    schedule_zh: "每天 17:30 (台北)",
  },
};

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
  const qc = useQueryClient();
  const { data: serverRows = [], isLoading } = useQuery<IngestHealth[]>({
    queryKey: ["admin", "ingest-health"],
    queryFn: () => api.get("/admin/ingest/health").then((r) => r.data),
    refetchInterval: 60_000,
  });
  // Union with the whitelist so newly-deployed jobs that haven't
  // hit their first cron tick yet still appear in the table — admin
  // can fire the first run manually via "Retry now" instead of
  // waiting for 09:30 UTC for the row to materialise. Placeholder
  // rows have `last_run_at=null` which renders as "—" via timeAgo().
  const rows: IngestHealth[] = (() => {
    const seen = new Set(serverRows.map((r) => r.job_id));
    const placeholders: IngestHealth[] = [];
    for (const jobId of RETRYABLE_INGEST_JOBS) {
      if (!seen.has(jobId)) {
        placeholders.push({
          job_id: jobId,
          last_run_at: null,
          ok: false,
          row_count: 0,
          error: null,
        });
      }
    }
    return [...serverRows, ...placeholders];
  })();
  const retry = useMutation({
    mutationFn: (jobId: string) =>
      api.post<IngestRetryResult>(`/admin/ingest/${jobId}/retry`).then((r) => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "ingest-health"] }),
  });
  const retryingJobId = retry.isPending ? retry.variables : null;

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-medium">Scheduled Ingest Health</h2>
        <span className="text-[10px] text-muted-foreground">
          refreshes every 60s
        </span>
      </div>
      {retry.isSuccess && (
        <p className="text-xs text-green-500">{retry.data.message}</p>
      )}
      {retry.isError && (
        <p className="text-xs text-red-500">Retry request failed. Please try again.</p>
      )}

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
                <th className="text-left py-1.5 pr-3">排程</th>
                <th className="text-left py-1.5 pr-3">Status</th>
                <th className="text-right py-1.5 pr-3">Rows</th>
                <th className="text-left py-1.5 pr-3">Last Run</th>
                <th className="text-left py-1.5">Error</th>
                <th className="text-right py-1.5 pl-3">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {rows.map((r) => {
                // Three-state badge: ok / error / pending. `last_run_at
                // === null` means the job has never actually run, so a
                // red "error" pill would be misleading — it's just
                // never-run-yet (e.g. a newly-deployed cron before the
                // first scheduled tick).
                const neverRun = r.last_run_at === null;
                const badgeCls = neverRun
                  ? "bg-muted/30 text-muted-foreground border border-border"
                  : r.ok
                    ? "bg-green-500/10 text-green-400 border border-green-500/30"
                    : "bg-red-500/10 text-red-400 border border-red-500/30";
                const badgeText = neverRun
                  ? "pending"
                  : r.ok ? "ok" : "error";
                const meta = JOB_META[r.job_id];
                return (
                <tr key={r.job_id}>
                  <td className="py-1.5 pr-3 align-top">
                    <div className="font-mono">{r.job_id}</div>
                    {meta && (
                      <div className="text-[10px] text-muted-foreground/80 mt-0.5">
                        {meta.description_zh}
                      </div>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 text-muted-foreground align-top whitespace-nowrap">
                    {meta?.schedule_zh ?? "—"}
                  </td>
                  <td className="py-1.5 pr-3 align-top">
                    <span
                      className={`px-1.5 py-0.5 rounded text-[10px] ${badgeCls}`}
                    >
                      {badgeText}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums align-top">
                    {r.row_count.toLocaleString()}
                  </td>
                  <td className="py-1.5 pr-3 text-muted-foreground align-top">
                    {timeAgo(r.last_run_at)}
                  </td>
                  <td
                    className="py-1.5 text-muted-foreground truncate max-w-[24rem] align-top"
                    title={r.error ?? undefined}
                  >
                    {r.error ?? ""}
                  </td>
                  <td className="py-1.5 pl-3 text-right align-top">
                    {RETRYABLE_INGEST_JOBS.has(r.job_id) ? (
                      <button
                        type="button"
                        disabled={retry.isPending}
                        onClick={() => retry.mutate(r.job_id)}
                        title="Clear backoff and queue one immediate run"
                        className="px-2 py-1 rounded border border-border bg-background hover:bg-accent/10 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                      >
                        {retryingJobId === r.job_id ? "Retrying..." : "Retry now"}
                      </button>
                    ) : (
                      <span className="text-muted-foreground">-</span>
                    )}
                  </td>
                </tr>
                );
              })}
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

      <SystemTasksCard />

      <RuntimeTunablesCard />

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
