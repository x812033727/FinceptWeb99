import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import api, { errorDetail } from "@/lib/api";
import { formatTaipei } from "@/lib/timeFormat";
import { type DataTableColumn } from "../ui/table";

import { IssueKeyForm } from "./FinmindKeys/issueForm";
import { IssuedKeyBanner } from "./FinmindKeys/issuedBanner";
import { KeysTable } from "./FinmindKeys/keysTable";
import { PlansSection } from "./FinmindKeys/plans";
import {
  type ApiKeyItem,
  type IssueKeyInput,
  type IssuedKeyResponse,
  type PlanFormState,
  type PlanItem,
  type PlanUpsertInput,
} from "./FinmindKeys/types";

// Plaintext key auto-clear delay. Long enough for the operator to
// copy + send to the customer; short enough that walking away from
// the desk doesn't leave a credential lingering in the DOM forever.
const PLAINTEXT_AUTO_CLEAR_MS = 120_000; // 2 minutes

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
  const [planForm, setPlanForm] = useState<PlanFormState>({
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
    mutationFn: async (input: IssueKeyInput) => {
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
    mutationFn: async (input: PlanUpsertInput) => {
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

  const planColumns: DataTableColumn<PlanItem>[] = [
    {
      key: "code",
      header: "Code",
      render: (p) => <span className="font-mono">{p.code}</span>,
    },
    { key: "name", header: "Name", render: (p) => p.name },
    {
      key: "price_monthly",
      header: "TWD/mo",
      numeric: true,
      render: (p) => p.price_monthly ?? "—",
    },
    {
      key: "quota_daily_calls",
      header: "Calls/d",
      numeric: true,
      render: (p) => p.quota_daily_calls.toLocaleString(),
    },
    {
      key: "quota_daily_rows",
      header: "Rows/d",
      numeric: true,
      render: (p) => p.quota_daily_rows.toLocaleString(),
    },
    {
      key: "status",
      header: "Status",
      render: (p) =>
        p.enabled ? (
          <span className="text-success">enabled</span>
        ) : (
          <span className="text-muted-foreground">disabled</span>
        ),
    },
    {
      key: "actions",
      header: "",
      render: (p) =>
        p.enabled ? (
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
        ) : null,
    },
  ];

  const keyColumns: DataTableColumn<ApiKeyItem>[] = [
    {
      key: "prefix",
      header: "Prefix",
      render: (k) => <span className="font-mono">{k.prefix}…</span>,
    },
    { key: "owner", header: "Owner", render: (k) => k.owner_email },
    {
      key: "name",
      header: "Name",
      render: (k) => (
        <span className="text-muted-foreground">{k.name || "—"}</span>
      ),
    },
    {
      key: "plan",
      header: "Plan",
      render: (k) =>
        k.plan_code ? (
          <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">
            {k.plan_code}
          </span>
        ) : (
          <span className="text-muted-foreground">free</span>
        ),
    },
    {
      key: "status",
      header: "Status",
      render: (k) =>
        k.enabled ? (
          <span className="text-success">enabled</span>
        ) : (
          <span className="text-muted-foreground">revoked</span>
        ),
    },
    {
      key: "last_used",
      header: "Last used",
      render: (k) => (
        <span className="text-muted-foreground">
          {k.last_used_at ? formatTaipei(k.last_used_at) : "never"}
        </span>
      ),
    },
    {
      key: "created",
      header: "Created",
      render: (k) => (
        <span className="text-muted-foreground">
          {formatTaipei(k.created_at, "date")}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (k) =>
        k.enabled ? (
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
        ) : null,
    },
  ];

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
          <PlansSection
            planForm={planForm}
            setPlanForm={setPlanForm}
            upsertPlanMutation={upsertPlanMutation}
            plansQuery={plansQuery}
            planColumns={planColumns}
          />

          {/* Issue form ────────────────────────── */}
          <div className="rounded border border-border bg-muted/20 p-3">
            <h3 className="mb-2 text-sm font-semibold">Issue new key</h3>
            <IssueKeyForm
              ownerEmail={ownerEmail}
              setOwnerEmail={setOwnerEmail}
              keyName={keyName}
              setKeyName={setKeyName}
              keyPlanCode={keyPlanCode}
              setKeyPlanCode={setKeyPlanCode}
              issueMutation={issueMutation}
              plansQuery={plansQuery}
            />

            <IssuedKeyBanner
              issuedKey={issuedKey}
              setIssuedKey={setIssuedKey}
            />
            {issueMutation.isError && (
              <div className="mt-2 text-xs text-destructive">
                Failed:{" "}
                {errorDetail(issueMutation.error)}
              </div>
            )}
          </div>

          {/* Keys table ─────────────────────────── */}
          <KeysTable keysQuery={keysQuery} keyColumns={keyColumns} />
        </div>
      )}
    </div>
  );
}
