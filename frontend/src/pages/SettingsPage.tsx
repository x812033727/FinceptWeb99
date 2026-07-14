import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Bell, BellOff, Moon, Shield, Sun, X } from "lucide-react";
import api from "@/lib/api";
import { disablePush, enablePush, getPushStatus, type PushStatus } from "@/lib/webPush";
import { useAuthStore } from "@/store/authStore";
import { useThemeStore } from "@/store/themeStore";
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
    <div className="bg-card shadow-highlight border border-border rounded-lg p-5 space-y-4">
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
  const { t, i18n } = useTranslation();
  const role = useAuthStore((s) => s.user?.role);
  const {
    theme,
    toggle: toggleTheme,
    marketColorMode,
    setMarketColorMode,
    density,
    setDensity,
  } = useThemeStore();

  const { data: me } = useQuery<UserProfile>({
    queryKey: ["me"],
    queryFn: () => api.get("/auth/me").then((r) => r.data),
  });

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
    onError: (err: Error) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setPwError(detail ?? "Failed to change password.");
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

  // ── Web Push (D3 瀏覽器推播) ───────────────────────────────────
  const [pushStatus, setPushStatus] = useState<PushStatus | "loading">("loading");
  const [pushBusy, setPushBusy] = useState(false);
  const [pushError, setPushError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void getPushStatus().then((s) => {
      if (!cancelled) setPushStatus(s);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handlePushToggle() {
    setPushBusy(true);
    setPushError(false);
    try {
      const next = pushStatus === "on" ? await disablePush() : await enablePush();
      setPushStatus(next);
    } catch {
      setPushError(true);
    } finally {
      setPushBusy(false);
    }
  }

  const pushToggleable = pushStatus === "on" || pushStatus === "off" || pushStatus === "unconfigured";

  return (
    <div className="p-4 sm:p-6 space-y-5 max-w-2xl">
      <h1 className="text-title font-semibold">Settings</h1>

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

      {/* Preferences */}
      <Section title={t("settings.preferences.title")}>
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium">{t("settings.preferences.theme")}</p>
              <p className="text-xs text-muted-foreground">
                {theme === "dark"
                  ? t("settings.preferences.theme_dark_desc")
                  : t("settings.preferences.theme_light_desc")}
              </p>
            </div>
            <button
              onClick={toggleTheme}
              className="px-3 py-1.5 rounded border border-border text-sm hover:bg-accent/10 transition-colors min-h-[36px] inline-flex items-center gap-2"
            >
              {theme === "dark" ? (
                <>
                  <Sun className="h-3.5 w-3.5" aria-hidden="true" />
                  {t("settings.preferences.switch_to_light")}
                </>
              ) : (
                <>
                  <Moon className="h-3.5 w-3.5" aria-hidden="true" />
                  {t("settings.preferences.switch_to_dark")}
                </>
              )}
            </button>
          </div>

          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium">{t("settings.preferences.language")}</p>
              <p className="text-xs text-muted-foreground">
                {i18n.language === "zh-TW" ? "繁體中文" : "English"}
              </p>
            </div>
            <button
              onClick={() => void i18n.changeLanguage(i18n.language === "zh-TW" ? "en" : "zh-TW")}
              className="px-3 py-1.5 rounded border border-border text-sm hover:bg-accent/10 transition-colors min-h-[36px]"
            >
              {i18n.language === "zh-TW" ? "Switch to English" : "切換為繁體中文"}
            </button>
          </div>

          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-sm font-medium">{t("settings.preferences.market_colors")}</p>
              <p className="text-xs text-muted-foreground">
                {t("settings.preferences.market_colors_desc")}
              </p>
            </div>
            <div className="flex rounded border border-border overflow-hidden shrink-0" role="group">
              {(["auto", "tw", "intl"] as const).map((mode) => (
                <button
                  key={mode}
                  onClick={() => setMarketColorMode(mode)}
                  aria-pressed={marketColorMode === mode}
                  className={`px-2.5 py-1.5 text-xs transition-colors min-h-[36px] ${
                    marketColorMode === mode
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground hover:bg-accent/10"
                  }`}
                >
                  {t(`settings.preferences.market_colors_${mode}`)}
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-sm font-medium">{t("settings.preferences.density")}</p>
              <p className="text-xs text-muted-foreground">
                {t("settings.preferences.density_desc")}
              </p>
            </div>
            <div className="flex rounded border border-border overflow-hidden shrink-0" role="group">
              {(["comfortable", "compact"] as const).map((d) => (
                <button
                  key={d}
                  onClick={() => setDensity(d)}
                  aria-pressed={density === d}
                  className={`px-2.5 py-1.5 text-xs transition-colors min-h-[36px] ${
                    density === d
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground hover:bg-accent/10"
                  }`}
                >
                  {t(`settings.preferences.density_${d}`)}
                </button>
              ))}
            </div>
          </div>
        </div>
      </Section>

      {/* Notifications (D3 Web Push) */}
      <Section title={t("settings.notifications.title")}>
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-sm font-medium">{t("settings.notifications.web_push")}</p>
            <p className="text-xs text-muted-foreground">
              {pushStatus === "unsupported"
                ? t("settings.notifications.unsupported")
                : pushStatus === "denied"
                ? t("settings.notifications.denied")
                : pushStatus === "unconfigured"
                ? t("settings.notifications.unconfigured")
                : pushStatus === "on"
                ? t("settings.notifications.web_push_on_desc")
                : t("settings.notifications.web_push_desc")}
            </p>
          </div>
          {pushStatus !== "loading" && pushStatus !== "unsupported" && pushStatus !== "denied" && (
            <button
              onClick={() => void handlePushToggle()}
              disabled={pushBusy || !pushToggleable}
              aria-pressed={pushStatus === "on"}
              className="px-3 py-1.5 rounded border border-border text-sm hover:bg-accent/10 transition-colors min-h-[36px] inline-flex items-center gap-2 disabled:opacity-50 shrink-0"
            >
              {pushStatus === "on" ? (
                <>
                  <BellOff className="h-3.5 w-3.5" aria-hidden="true" />
                  {pushBusy ? "…" : t("settings.notifications.disable")}
                </>
              ) : (
                <>
                  <Bell className="h-3.5 w-3.5" aria-hidden="true" />
                  {pushBusy ? "…" : t("settings.notifications.enable")}
                </>
              )}
            </button>
          )}
        </div>
        {pushError && (
          <p className="text-xs text-danger">{t("settings.notifications.error")}</p>
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
          {pwError && <p className="text-xs text-danger">{pwError}</p>}
          {pwSuccess && (
            <p className="text-xs text-success">Password changed successfully.</p>
          )}
          <button
            type="submit"
            disabled={changePw.isPending}
            className="px-4 py-1.5 rounded bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50 min-h-[36px]"
          >
            {changePw.isPending ? "Saving…" : "Update Password"}
          </button>
        </form>
      </Section>

      {/* API Keys */}
      <Section title="API Keys">
        {newKey && (
          <div className="bg-warning/10 border border-warning/30 rounded p-3 text-xs space-y-1">
            <p className="font-medium text-warning">
              Copy this key now — it will not be shown again.
            </p>
            <code className="block break-all text-foreground">{newKey}</code>
            <button
              className="text-muted-foreground hover:text-foreground mt-1 min-h-[28px]"
              onClick={() => setNewKey(null)}
            >
              Dismiss
            </button>
          </div>
        )}

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
            className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50 min-h-[36px]"
          >
            {createKey.isPending ? "…" : "Generate"}
          </button>
        </div>
        {keyError && <p className="text-xs text-danger">{keyError}</p>}

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
                  aria-label="Delete API key"
                  className="text-muted-foreground hover:text-danger transition-colors ml-3 min-h-[32px] min-w-[32px] flex items-center justify-center rounded hover:bg-accent/10"
                >
                  <X className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <UsageCard scope="me" />

      {/* Admin shortcut — only rendered for admins; non-disruptive footer link. */}
      {role === "admin" && (
        <div className="text-center pt-2">
          <Link
            to="/admin"
            className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            <Shield className="h-3.5 w-3.5" aria-hidden="true" />
            {t("settings.admin_link")} →
          </Link>
        </div>
      )}
    </div>
  );
}
