import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { UsageCard } from "@/components/admin/UsageCard";

interface UserProfile {
  id: string;
  email: string;
  role: string;
  created_at: string;
  ai_requests_remaining: number | null;
}

interface ApiKey {
  id: string;
  name: string;
  last_used_at: string | null;
  expires_at: string | null;
  created_at: string;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-4">
      <h2 className="text-sm font-semibold">{title}</h2>
      {children}
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  );
}

export default function SettingsPage() {
  const qc = useQueryClient();

  // ── Profile ───────────────────────────────────────────────────
  const { data: me } = useQuery<UserProfile>({
    queryKey: ["me"],
    queryFn: () => api.get("/auth/me").then((r) => r.data),
  });

  // ── Change password ───────────────────────────────────────────
  const [pwForm, setPwForm] = useState({ current: "", next: "", confirm: "" });
  const [pwError, setPwError] = useState("");
  const [pwSuccess, setPwSuccess] = useState(false);

  const changePw = useMutation({
    mutationFn: (body: { current_password: string; new_password: string }) =>
      api.patch("/auth/me", body),
    onSuccess: () => {
      setPwForm({ current: "", next: "", confirm: "" });
      setPwError("");
      setPwSuccess(true);
      setTimeout(() => setPwSuccess(false), 3000);
    },
    onError: (err: any) => {
      setPwError(err?.response?.data?.detail ?? "Failed to change password.");
    },
  });

  function handlePwSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (pwForm.next !== pwForm.confirm) {
      setPwError("New passwords do not match.");
      return;
    }
    if (pwForm.next.length < 8) {
      setPwError("Password must be at least 8 characters.");
      return;
    }
    changePw.mutate({ current_password: pwForm.current, new_password: pwForm.next });
  }

  // ── API Keys ──────────────────────────────────────────────────
  const { data: apiKeys = [] } = useQuery<ApiKey[]>({
    queryKey: ["api-keys"],
    queryFn: () => api.get("/auth/api-keys").then((r) => r.data),
  });

  const [keyName, setKeyName] = useState("");
  const [newKey, setNewKey] = useState<string | null>(null);
  const [keyError, setKeyError] = useState<string | null>(null);

  const createKey = useMutation({
    mutationFn: (name: string) => api.post("/auth/api-keys", { name }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["api-keys"] });
      setNewKey(r.data.key);
      setKeyName("");
      setKeyError(null);
    },
    onError: (err: Error) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setKeyError(detail ?? "Failed to create API key");
    },
  });

  const deleteKey = useMutation({
    mutationFn: (id: string) => api.delete(`/auth/api-keys/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["api-keys"] }),
  });

  return (
    <div className="p-4 sm:p-6 space-y-5 max-w-2xl">
      <h1 className="text-xl sm:text-2xl font-semibold">Settings</h1>

      {/* Profile */}
      <Section title="Profile">
        {me && (
          <div className="space-y-2">
            <Field label="Email" value={me.email} />
            <Field label="Role" value={me.role} />
            <Field
              label="Member since"
              value={new Date(me.created_at).toLocaleDateString()}
            />
            {me.ai_requests_remaining != null && (
              <Field
                label="AI requests remaining today"
                value={String(me.ai_requests_remaining)}
              />
            )}
          </div>
        )}
      </Section>

      {/* Change password */}
      <Section title="Change Password">
        <form onSubmit={handlePwSubmit} className="space-y-3">
          {(["current", "next", "confirm"] as const).map((field) => (
            <div key={field} className="space-y-1">
              <label className="text-xs text-muted-foreground capitalize">
                {field === "current"
                  ? "Current password"
                  : field === "next"
                  ? "New password"
                  : "Confirm new password"}
              </label>
              <input
                type="password"
                className="w-full bg-background border border-border rounded px-3 py-1.5 text-sm"
                value={pwForm[field]}
                onChange={(e) =>
                  setPwForm((f) => ({ ...f, [field]: e.target.value }))
                }
                autoComplete={field === "current" ? "current-password" : "new-password"}
              />
            </div>
          ))}
          {pwError && <p className="text-xs text-red-400">{pwError}</p>}
          {pwSuccess && (
            <p className="text-xs text-green-400">Password changed successfully.</p>
          )}
          <button
            type="submit"
            disabled={changePw.isPending}
            className="px-4 py-1.5 rounded bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
          >
            {changePw.isPending ? "Saving…" : "Update Password"}
          </button>
        </form>
      </Section>

      {/* API Keys */}
      <Section title="API Keys">
        {newKey && (
          <div className="bg-amber-500/10 border border-amber-500/30 rounded p-3 text-xs space-y-1">
            <p className="font-medium text-amber-400">
              Copy this key now — it will not be shown again.
            </p>
            <code className="block break-all text-foreground">{newKey}</code>
            <button
              className="text-muted-foreground hover:text-foreground mt-1"
              onClick={() => setNewKey(null)}
            >
              Dismiss
            </button>
          </div>
        )}

        {/* Create */}
        <div className="flex gap-2">
          <input
            className="flex-1 bg-background border border-border rounded px-3 py-1.5 text-sm"
            placeholder="Key name"
            value={keyName}
            onChange={(e) => setKeyName(e.target.value)}
          />
          <button
            onClick={() => keyName.trim() && createKey.mutate(keyName.trim())}
            disabled={createKey.isPending || !keyName.trim()}
            className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
          >
            {createKey.isPending ? "…" : "Generate"}
          </button>
        </div>
        {keyError && <p className="text-xs text-red-400">{keyError}</p>}

        {/* List */}
        {apiKeys.length === 0 ? (
          <p className="text-xs text-muted-foreground">No API keys yet.</p>
        ) : (
          <ul className="space-y-1.5">
            {apiKeys.map((k) => (
              <li
                key={k.id}
                className="flex items-center justify-between text-xs px-3 py-2 rounded border border-border bg-background"
              >
                <div className="space-y-0.5">
                  <span className="font-medium">{k.name}</span>
                  <p className="text-muted-foreground">
                    Created {new Date(k.created_at).toLocaleDateString()}
                    {k.last_used_at &&
                      ` · Last used ${new Date(k.last_used_at).toLocaleDateString()}`}
                    {k.expires_at && ` · Expires ${new Date(k.expires_at).toLocaleDateString()}`}
                  </p>
                </div>
                <button
                  onClick={() => deleteKey.mutate(k.id)}
                  className="text-muted-foreground hover:text-red-400 transition-colors text-base leading-none ml-3"
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <MyLLMKeysSection />

      <UsageCard scope="me" />
    </div>
  );
}


// ── My LLM provider keys (per-user, overrides system-wide) ────────

interface MyLLMKey {
  provider: string;
  has_key: boolean;
  source: "db" | "env" | "none";
  masked: string;
  last_validated_at: string | null;
  last_validation_ok: boolean | null;
  last_validation_message: string | null;
  updated_at: string | null;
}

const MY_LLM_PROVIDERS: Record<string, { name: string; placeholder: string }> = {
  openai:    { name: "ChatGPT (OpenAI)", placeholder: "sk-…" },
  anthropic: { name: "Claude (Anthropic)", placeholder: "sk-ant-api…" },
  gemini:    { name: "Gemini (Google)", placeholder: "AIza…" },
  minimax:   { name: "MiniMax", placeholder: "eyJhbG…" },
};

function MyKeyRow({ info }: { info: MyLLMKey }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const meta = MY_LLM_PROVIDERS[info.provider] ?? { name: info.provider, placeholder: "" };
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => api.put(`/auth/llm-keys/${info.provider}`, { api_key: draft }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["my-llm-keys"] });
      setDraft("");
      setError(null);
    },
    onError: (err: Error) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? "save failed");
    },
  });

  const clear = useMutation({
    mutationFn: () => api.delete(`/auth/llm-keys/${info.provider}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["my-llm-keys"] }),
  });

  const test = useMutation<{ data: { ok: boolean; message: string } }>({
    mutationFn: () => api.post(`/auth/llm-keys/${info.provider}/test`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["my-llm-keys"] }),
  });

  const testResult = test.data?.data;

  return (
    <div className="border border-border rounded p-3 space-y-2">
      <div className="flex justify-between items-baseline">
        <span className="font-medium text-sm">{meta.name}</span>
        {info.has_key && info.source === "db" && (
          <span className="text-xs text-muted-foreground font-mono">{info.masked}</span>
        )}
        {!info.has_key && (
          <span className="text-[10px] border border-border text-muted-foreground px-1.5 py-0.5 rounded">
            {t("my_keys.fallback_to_system")}
          </span>
        )}
      </div>
      <div className="flex gap-2 flex-wrap">
        <input
          type="password"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={info.has_key ? t("llm_keys.replace_placeholder") : meta.placeholder}
          className="flex-1 min-w-[180px] bg-background border border-border rounded px-2 py-1.5 text-xs font-mono"
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
            className="px-3 py-1.5 text-xs border border-border text-muted-foreground rounded hover:text-red-400"
          >
            {t("llm_keys.clear")}
          </button>
        )}
        <button
          onClick={() => test.mutate()}
          disabled={!info.has_key && info.source !== "db"}
          className="px-3 py-1.5 text-xs border border-border text-muted-foreground rounded hover:text-foreground disabled:opacity-40"
        >
          {test.isPending ? t("llm_keys.testing") : t("llm_keys.test")}
        </button>
      </div>
      {error && <span className="text-xs text-red-400">{error}</span>}
      {testResult && (
        <span className={`text-xs ${testResult.ok ? "text-green-400" : "text-red-400"}`}>
          {testResult.ok ? `✓ ${t("llm_keys.validated")}` : `✗ ${testResult.message.slice(0, 80)}`}
        </span>
      )}
    </div>
  );
}

function MyLLMKeysSection() {
  const { t } = useTranslation();
  const { data: keys = [], isLoading } = useQuery<MyLLMKey[]>({
    queryKey: ["my-llm-keys"],
    queryFn: () => api.get("/auth/llm-keys").then((r) => r.data),
  });

  return (
    <Section title={t("my_keys.title")}>
      <p className="text-xs text-muted-foreground -mt-2">{t("my_keys.subtitle")}</p>
      {isLoading ? (
        <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>
      ) : (
        <div className="space-y-2">
          {keys.map((k) => <MyKeyRow key={k.provider} info={k} />)}
        </div>
      )}
    </Section>
  );
}
