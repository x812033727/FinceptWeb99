import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { CollapsibleHeader, useCollapsible } from "@/components/Collapsible";
import api, { errorDetail } from "@/lib/api";

// Plaintext key auto-clear delay. Long enough for the operator to
// copy + send to the customer; short enough that walking away from
// the desk doesn't leave a credential lingering in the DOM forever.
const PLAINTEXT_AUTO_CLEAR_MS = 120_000; // 2 minutes

// Loose email regex — same shape as HTML5 input[type=email] uses
// for client-side validation. The backend re-validates anyway, so
// this is purely UX (disable submit + show inline hint).
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/**
 * AdminPage card for issuing + listing + revoking customer API keys.
 *
 * Plaintext is only readable ONCE at issuance — `IssuedKeyResponse`
 * arrives in the POST response and the operator must copy it
 * immediately. After that, only the prefix is shown anywhere; the
 * sha256 is never exposed.
 *
 * Soft-revoke: DELETE flips `enabled=false` rather than removing
 * the row, so api_usage_events FK references stay valid + the audit
 * trail (issued at / revoked at) survives.
 */

interface ApiKeyItem {
  id: number;
  prefix: string;
  owner_email: string;
  name: string | null;
  enabled: boolean;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
  plan_code: string | null;
  subscription_id: number | null;
}

interface IssuedKeyResponse {
  record_id: number;
  plaintext: string;
  prefix: string;
  owner_email: string;
  plan_code: string | null;
  subscription_id: number | null;
}

interface PlanItem {
  code: string;
  name: string;
  price_monthly: number | null;
  price_yearly: number | null;
  currency: string;
  allowed_datasets: string[] | null;
  quota_daily_calls: number;
  quota_daily_rows: number;
  enabled: boolean;
}

export default function FinmindKeysCard() {
  const { open, toggle } = useCollapsible("admin-finmind-keys", false);
  const queryClient = useQueryClient();

  const [ownerEmail, setOwnerEmail] = useState("");
  const [keyName, setKeyName] = useState("");
  const [keyPlanCode, setKeyPlanCode] = useState<string>("");
  // Plaintext from the most recent issuance — sticks until operator
  // dismisses, issues another key, or PLAINTEXT_AUTO_CLEAR_MS elapses.
  // NEVER persisted anywhere.
  const [issuedKey, setIssuedKey] = useState<IssuedKeyResponse | null>(null);

  // Auto-clear the plaintext after the timeout so an unattended
  // browser tab doesn't leave a credential visible indefinitely.
  // Resets on every new issuance; manual dismissal short-circuits.
  useEffect(() => {
    if (!issuedKey) return;
    const t = setTimeout(
      () => setIssuedKey(null),
      PLAINTEXT_AUTO_CLEAR_MS,
    );
    return () => clearTimeout(t);
  }, [issuedKey]);

  // Plan management — inline mini-form so the operator can create a
  // plan without opening a separate modal / page. Plans are
  // operator-managed metadata; customers never see this UI.
  const [planForm, setPlanForm] = useState({
    code: "",
    name: "",
    price_monthly: "",
    quota_daily_calls: "1000",
    quota_daily_rows: "100000",
  });

  const keysQuery = useQuery<ApiKeyItem[]>({
    queryKey: ["admin", "finmind", "keys"],
    queryFn: async () => {
      const r = await api.get<ApiKeyItem[]>("/admin/finmind/keys");
      return r.data;
    },
    enabled: open,
  });

  const plansQuery = useQuery<PlanItem[]>({
    queryKey: ["admin", "finmind", "plans"],
    queryFn: async () => {
      const r = await api.get<PlanItem[]>("/admin/finmind/plans");
      return r.data;
    },
    enabled: open,
  });

  const issueMutation = useMutation({
    mutationFn: async (input: {
      owner_email: string;
      name?: string;
      plan_code?: string;
    }) => {
      const r = await api.post<IssuedKeyResponse>(
        "/admin/finmind/keys",
        input,
      );
      return r.data;
    },
    onSuccess: (data) => {
      setIssuedKey(data);
      setOwnerEmail("");
      setKeyName("");
      setKeyPlanCode("");
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "keys"],
      });
    },
  });

  const upsertPlanMutation = useMutation({
    mutationFn: async (input: {
      code: string;
      name: string;
      price_monthly: number | null;
      quota_daily_calls: number;
      quota_daily_rows: number;
    }) => {
      const r = await api.put<PlanItem>(
        `/admin/finmind/plans/${input.code}`,
        {
          name: input.name,
          price_monthly: input.price_monthly,
          currency: "TWD",
          quota_daily_calls: input.quota_daily_calls,
          quota_daily_rows: input.quota_daily_rows,
          enabled: true,
        },
      );
      return r.data;
    },
    onSuccess: () => {
      setPlanForm({
        code: "",
        name: "",
        price_monthly: "",
        quota_daily_calls: "1000",
        quota_daily_rows: "100000",
      });
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "plans"],
      });
    },
  });

  const disablePlanMutation = useMutation({
    mutationFn: async (code: string) => {
      await api.delete(`/admin/finmind/plans/${code}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "plans"],
      });
    },
  });

  const revokeMutation = useMutation({
    mutationFn: async (id: number) => {
      await api.delete(`/admin/finmind/keys/${id}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "keys"],
      });
    },
  });

  const enabledCount =
    (keysQuery.data ?? []).filter((k) => k.enabled).length;

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <CollapsibleHeader
        title="FinMind API Keys"
        subtitle={
          keysQuery.data
            ? `${enabledCount} active · ${keysQuery.data.length} total`
            : "click to expand"
        }
        open={open}
        toggle={toggle}
      />

      {open && (
        <div className="mt-4 space-y-6">
          {/* Plans section (above the issue form so the operator
              creates a plan before issuing keys to use it) ───── */}
          <div className="rounded border border-border bg-muted/10 p-3">
            <h3 className="mb-2 text-sm font-semibold">Plans</h3>
            <form
              className="flex flex-wrap items-end gap-2 text-sm"
              onSubmit={(e) => {
                e.preventDefault();
                if (!planForm.code.trim() || !planForm.name.trim()) return;
                upsertPlanMutation.mutate({
                  code: planForm.code.trim(),
                  name: planForm.name.trim(),
                  price_monthly: planForm.price_monthly
                    ? Number(planForm.price_monthly)
                    : null,
                  quota_daily_calls: Number(planForm.quota_daily_calls) || 0,
                  quota_daily_rows: Number(planForm.quota_daily_rows) || 0,
                });
              }}
            >
              <label className="flex flex-col">
                <span className="text-xs text-muted-foreground">Code</span>
                <input
                  type="text"
                  required
                  value={planForm.code}
                  onChange={(e) =>
                    setPlanForm({ ...planForm, code: e.target.value })
                  }
                  className="w-24 rounded border border-border bg-background px-2 py-1"
                  placeholder="pro"
                />
              </label>
              <label className="flex flex-col">
                <span className="text-xs text-muted-foreground">Name</span>
                <input
                  type="text"
                  required
                  value={planForm.name}
                  onChange={(e) =>
                    setPlanForm({ ...planForm, name: e.target.value })
                  }
                  className="rounded border border-border bg-background px-2 py-1"
                  placeholder="Pro"
                />
              </label>
              <label className="flex flex-col">
                <span className="text-xs text-muted-foreground">
                  TWD/month
                </span>
                <input
                  type="number"
                  value={planForm.price_monthly}
                  onChange={(e) =>
                    setPlanForm({
                      ...planForm,
                      price_monthly: e.target.value,
                    })
                  }
                  className="w-24 rounded border border-border bg-background px-2 py-1"
                  placeholder="990"
                />
              </label>
              <label className="flex flex-col">
                <span className="text-xs text-muted-foreground">
                  Calls/day
                </span>
                <input
                  type="number"
                  value={planForm.quota_daily_calls}
                  onChange={(e) =>
                    setPlanForm({
                      ...planForm,
                      quota_daily_calls: e.target.value,
                    })
                  }
                  className="w-24 rounded border border-border bg-background px-2 py-1"
                />
              </label>
              <label className="flex flex-col">
                <span className="text-xs text-muted-foreground">
                  Rows/day
                </span>
                <input
                  type="number"
                  value={planForm.quota_daily_rows}
                  onChange={(e) =>
                    setPlanForm({
                      ...planForm,
                      quota_daily_rows: e.target.value,
                    })
                  }
                  className="w-28 rounded border border-border bg-background px-2 py-1"
                />
              </label>
              <button
                type="submit"
                disabled={upsertPlanMutation.isPending}
                className="rounded border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
              >
                {upsertPlanMutation.isPending ? "Saving…" : "Save plan"}
              </button>
            </form>

            {plansQuery.data && plansQuery.data.length > 0 && (
              <table className="mt-3 w-full text-xs">
                <thead className="border-b border-border text-left text-muted-foreground">
                  <tr>
                    <th className="py-1 pr-2">Code</th>
                    <th className="py-1 pr-2">Name</th>
                    <th className="py-1 pr-2 text-right">TWD/mo</th>
                    <th className="py-1 pr-2 text-right">Calls/d</th>
                    <th className="py-1 pr-2 text-right">Rows/d</th>
                    <th className="py-1 pr-2">Status</th>
                    <th className="py-1 pr-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {plansQuery.data.map((p) => (
                    <tr
                      key={p.code}
                      className="border-b border-border last:border-b-0"
                    >
                      <td className="py-1 pr-2 font-mono">{p.code}</td>
                      <td className="py-1 pr-2">{p.name}</td>
                      <td className="py-1 pr-2 text-right">
                        {p.price_monthly ?? "—"}
                      </td>
                      <td className="py-1 pr-2 text-right">
                        {p.quota_daily_calls.toLocaleString()}
                      </td>
                      <td className="py-1 pr-2 text-right">
                        {p.quota_daily_rows.toLocaleString()}
                      </td>
                      <td className="py-1 pr-2">
                        {p.enabled ? (
                          <span className="text-green-600">enabled</span>
                        ) : (
                          <span className="text-muted-foreground">
                            disabled
                          </span>
                        )}
                      </td>
                      <td className="py-1 pr-2">
                        {p.enabled && (
                          <button
                            type="button"
                            onClick={() => {
                              if (
                                confirm(
                                  `Disable plan "${p.code}"? New keys ` +
                                    `won't be able to use it; existing ` +
                                    `subscriptions degrade to free-tier.`,
                                )
                              ) {
                                disablePlanMutation.mutate(p.code);
                              }
                            }}
                            className="rounded border border-destructive px-1.5 py-0.5 text-[10px] text-destructive hover:bg-destructive/10"
                          >
                            Disable
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* Issue form ────────────────────────── */}
          <div className="rounded border border-border bg-muted/20 p-3">
            <h3 className="mb-2 text-sm font-semibold">Issue new key</h3>
            <form
              className="flex flex-wrap items-end gap-2 text-sm"
              onSubmit={(e) => {
                e.preventDefault();
                const email = ownerEmail.trim();
                if (!email || !EMAIL_RE.test(email)) return;
                issueMutation.mutate({
                  owner_email: email,
                  name: keyName.trim() || undefined,
                  plan_code: keyPlanCode.trim() || undefined,
                });
              }}
            >
              <label className="flex flex-col">
                <span className="text-xs text-muted-foreground">
                  Owner email
                </span>
                <input
                  type="email"
                  required
                  value={ownerEmail}
                  onChange={(e) => setOwnerEmail(e.target.value)}
                  className="rounded border border-border bg-background px-2 py-1"
                  placeholder="customer@example.com"
                />
                {ownerEmail.trim().length > 0 &&
                  !EMAIL_RE.test(ownerEmail.trim()) && (
                    <span className="mt-0.5 text-[10px] text-destructive">
                      Invalid email format
                    </span>
                  )}
              </label>
              <label className="flex flex-col">
                <span className="text-xs text-muted-foreground">
                  Name (optional)
                </span>
                <input
                  type="text"
                  value={keyName}
                  onChange={(e) => setKeyName(e.target.value)}
                  className="rounded border border-border bg-background px-2 py-1"
                  placeholder="e.g. prod-backtest"
                />
              </label>
              <label className="flex flex-col">
                <span className="text-xs text-muted-foreground">Plan</span>
                <select
                  value={keyPlanCode}
                  onChange={(e) => setKeyPlanCode(e.target.value)}
                  className="rounded border border-border bg-background px-2 py-1"
                >
                  <option value="">Free tier (default)</option>
                  {(plansQuery.data ?? [])
                    .filter((p) => p.enabled)
                    .map((p) => (
                      <option key={p.code} value={p.code}>
                        {p.code} — {p.name}
                      </option>
                    ))}
                </select>
              </label>
              <button
                type="submit"
                disabled={
                  issueMutation.isPending ||
                  !ownerEmail.trim() ||
                  !EMAIL_RE.test(ownerEmail.trim())
                }
                className="rounded bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {issueMutation.isPending ? "Issuing…" : "Issue key"}
              </button>
            </form>

            {issuedKey && (
              <div className="mt-3 rounded border border-amber-400 bg-amber-50 p-3 text-xs dark:bg-amber-950">
                <div className="mb-1 font-semibold text-amber-800 dark:text-amber-200">
                  ⚠ Copy this key now — it will not be shown again.
                </div>
                <div className="break-all font-mono">
                  {issuedKey.plaintext}
                </div>
                <div className="mt-2 flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      navigator.clipboard?.writeText(issuedKey.plaintext);
                    }}
                    className="rounded border border-border bg-background px-2 py-0.5 text-[11px]"
                  >
                    Copy
                  </button>
                  <button
                    type="button"
                    onClick={() => setIssuedKey(null)}
                    className="rounded border border-border bg-background px-2 py-0.5 text-[11px]"
                  >
                    Dismiss
                  </button>
                  <span className="text-muted-foreground">
                    Issued for {issuedKey.owner_email} · prefix{" "}
                    <code>{issuedKey.prefix}</code> ·{" "}
                    {issuedKey.plan_code ? (
                      <>
                        plan <code>{issuedKey.plan_code}</code>
                      </>
                    ) : (
                      <>plan free-tier</>
                    )}
                  </span>
                </div>
              </div>
            )}
            {issueMutation.isError && (
              <div className="mt-2 text-xs text-destructive">
                Failed:{" "}
                {errorDetail(issueMutation.error)}
              </div>
            )}
          </div>

          {/* Keys table ─────────────────────────── */}
          {keysQuery.isLoading && (
            <div className="text-sm text-muted-foreground">Loading…</div>
          )}
          {keysQuery.data && keysQuery.data.length === 0 && (
            <div className="rounded border border-border bg-muted/30 p-6 text-center text-sm text-muted-foreground">
              No API keys yet. Issue one above to give a customer access
              to <code className="rounded bg-muted px-1">/api/finmind/data/...</code>
              .
            </div>
          )}
          {keysQuery.data && keysQuery.data.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="border-b border-border text-left text-muted-foreground">
                  <tr>
                    <th className="py-1.5 pr-2">Prefix</th>
                    <th className="py-1.5 pr-2">Owner</th>
                    <th className="py-1.5 pr-2">Name</th>
                    <th className="py-1.5 pr-2">Plan</th>
                    <th className="py-1.5 pr-2">Status</th>
                    <th className="py-1.5 pr-2">Last used</th>
                    <th className="py-1.5 pr-2">Created</th>
                    <th className="py-1.5 pr-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {keysQuery.data.map((k) => (
                    <tr
                      key={k.id}
                      className="border-b border-border last:border-b-0"
                    >
                      <td className="py-1.5 pr-2 font-mono">{k.prefix}…</td>
                      <td className="py-1.5 pr-2">{k.owner_email}</td>
                      <td className="py-1.5 pr-2 text-muted-foreground">
                        {k.name || "—"}
                      </td>
                      <td className="py-1.5 pr-2">
                        {k.plan_code ? (
                          <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">
                            {k.plan_code}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">free</span>
                        )}
                      </td>
                      <td className="py-1.5 pr-2">
                        {k.enabled ? (
                          <span className="text-green-600">enabled</span>
                        ) : (
                          <span className="text-muted-foreground">
                            revoked
                          </span>
                        )}
                      </td>
                      <td className="py-1.5 pr-2 text-muted-foreground">
                        {k.last_used_at
                          ? new Date(k.last_used_at).toLocaleString()
                          : "never"}
                      </td>
                      <td className="py-1.5 pr-2 text-muted-foreground">
                        {new Date(k.created_at).toLocaleDateString()}
                      </td>
                      <td className="py-1.5 pr-2">
                        {k.enabled && (
                          <button
                            type="button"
                            onClick={() => {
                              if (
                                confirm(
                                  `Revoke key for ${k.owner_email}? ` +
                                    `Their integration will start getting 401s immediately.`,
                                )
                              ) {
                                revokeMutation.mutate(k.id);
                              }
                            }}
                            disabled={revokeMutation.isPending}
                            className="rounded border border-destructive px-1.5 py-0.5 text-[10px] text-destructive hover:bg-destructive/10 disabled:opacity-50"
                          >
                            Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
