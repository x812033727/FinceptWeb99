import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";

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

  const createKey = useMutation({
    mutationFn: (name: string) => api.post("/auth/api-keys", { name }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["api-keys"] });
      setNewKey(r.data.key);
      setKeyName("");
    },
  });

  const deleteKey = useMutation({
    mutationFn: (id: string) => api.delete(`/auth/api-keys/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["api-keys"] }),
  });

  return (
    <div className="p-6 space-y-5 max-w-2xl">
      <h1 className="text-xl font-semibold">Settings</h1>

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
            Generate
          </button>
        </div>

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
    </div>
  );
}
