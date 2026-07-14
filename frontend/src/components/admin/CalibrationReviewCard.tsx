import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Shield } from "lucide-react";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import api from "@/lib/api";
import { formatTaipei } from "@/lib/timeFormat";
import { DataTable, type DataTableColumn } from "../ui/table";

interface PendingCalibration {
  strategy_id: string;
  live_curve: { raw: number; calibrated: number }[];
  live_updated_at: string | null;
  live_sample_count: number | null;
  pending_curve: { raw: number; calibrated: number }[] | null;
  pending_reason: string | null;
  pending_at: string | null;
}

interface StrategySummary {
  id: string;
  name: string;
  market: string;
}

/**
 * PR-3: AdminPage card listing strategies with a freshly-fitted
 * calibration curve queued for review (safety check failed in the
 * sweep Phase 3 fit). Each row shows the live-vs-pending curve diff
 * + the gate's failure reason + Approve / Reject buttons.
 *
 * Hides itself entirely when no strategies have a pending curve so
 * the AdminPage doesn't grow noise when everything is clean.
 */
export function CalibrationReviewCard() {
  const { open, toggle } = useCollapsible("admin.calibration-review");
  const qc = useQueryClient();

  const strategiesQ = useQuery({
    queryKey: ["admin", "calibration-pending-strategies"],
    queryFn: async () => {
      const { data } = await api.get<StrategySummary[]>("/discussion/strategies");
      return data;
    },
    staleTime: 30_000,
  });

  const pendingQ = useQuery({
    queryKey: ["admin", "calibration-pending-payloads", strategiesQ.data?.map((s: StrategySummary) => s.id)],
    enabled: !!strategiesQ.data && strategiesQ.data.length > 0,
    queryFn: async () => {
      if (!strategiesQ.data) return [];
      const out: { strategy: StrategySummary; payload: PendingCalibration }[] = [];
      for (const s of strategiesQ.data) {
        try {
          const { data } = await api.get<PendingCalibration>(
            `/discussion/strategies/${s.id}/calibration/pending`,
          );
          if (data.pending_curve) {
            out.push({ strategy: s, payload: data });
          }
        } catch {
          // 403/404 → skip silently, the strategy isn't ours or is gone
        }
      }
      return out;
    },
    refetchInterval: 60_000,
  });

  const approveMut = useMutation({
    mutationFn: async (sid: string) =>
      api.post(`/discussion/strategies/${sid}/calibration/approve`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "calibration-pending-payloads"] });
    },
  });
  const rejectMut = useMutation({
    mutationFn: async (sid: string) =>
      api.post(`/discussion/strategies/${sid}/calibration/reject`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "calibration-pending-payloads"] });
    },
  });

  const queue = pendingQ.data ?? [];
  // Hide entirely when nothing queued — admin page stays clean
  if (!strategiesQ.isLoading && queue.length === 0) return null;

  const subtitle = pendingQ.isLoading
    ? <span className="animate-pulse">Loading…</span>
    : <span>{queue.length} strategy(ies) awaiting review</span>;

  return (
    <div className="bg-card border border-warning/30 rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title={
          <span className="inline-flex items-center gap-1.5">
            <Shield className="h-3.5 w-3.5" aria-hidden="true" />
            Calibration deployment gate
          </span>
        }
        subtitle={subtitle}
      />
      {open && (
        <div className="space-y-4">
          {queue.map(({ strategy, payload }) => (
            <PendingRow
              key={strategy.id}
              strategy={strategy}
              payload={payload}
              onApprove={() => approveMut.mutate(strategy.id)}
              onReject={() => rejectMut.mutate(strategy.id)}
              busy={approveMut.isPending || rejectMut.isPending}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function PendingRow({
  strategy,
  payload,
  onApprove,
  onReject,
  busy,
}: {
  strategy: StrategySummary;
  payload: PendingCalibration;
  onApprove: () => void;
  onReject: () => void;
  busy: boolean;
}) {
  const live = payload.live_curve ?? [];
  const pending = payload.pending_curve ?? [];

  // Build a side-by-side diff at common raw bins. Use the union of
  // raw values from both curves so the table reflects every shift.
  const allRaws = Array.from(
    new Set([...live, ...pending].map((p) => p.raw)),
  ).sort((a, b) => a - b);
  const liveAt = (raw: number) =>
    live.find((p) => Math.abs(p.raw - raw) < 1e-9)?.calibrated;
  const pendingAt = (raw: number) =>
    pending.find((p) => Math.abs(p.raw - raw) < 1e-9)?.calibrated;

  const columns: DataTableColumn<number>[] = [
    {
      key: "raw",
      header: "raw",
      cellClassName: "font-mono",
      render: (raw) => raw.toFixed(2),
    },
    {
      key: "live",
      header: "live",
      numeric: true,
      render: (raw) => {
        const l = liveAt(raw);
        return l !== undefined ? l.toFixed(3) : "—";
      },
    },
    {
      key: "pending",
      header: "pending",
      numeric: true,
      render: (raw) => {
        const p = pendingAt(raw);
        return p !== undefined ? p.toFixed(3) : "—";
      },
    },
    {
      key: "delta",
      header: "Δ",
      numeric: true,
      render: (raw) => {
        const l = liveAt(raw);
        const p = pendingAt(raw);
        const delta = l !== undefined && p !== undefined ? p - l : null;
        return (
          <span
            className={
              delta === null
                ? "text-muted-foreground"
                : delta > 0
                  ? "text-success"
                  : delta < 0
                    ? "text-danger"
                    : ""
            }
          >
            {delta === null
              ? "—"
              : `${delta > 0 ? "+" : ""}${delta.toFixed(3)}`}
          </span>
        );
      },
    },
  ];

  return (
    <div className="border border-border rounded p-3 bg-background/40">
      <div className="flex items-center justify-between mb-2 gap-2">
        <div className="min-w-0">
          <div className="text-sm font-semibold truncate">
            {strategy.name}{" "}
            <span className="text-xs text-muted-foreground">
              ({strategy.market})
            </span>
          </div>
          <div className="text-meta text-warning truncate">
            {payload.pending_reason ?? "Pending review"}
          </div>
          {payload.pending_at && (
            <div className="text-micro text-muted-foreground">
              Queued: {formatTaipei(payload.pending_at)}
            </div>
          )}
        </div>
        <div className="flex gap-2 shrink-0">
          <button
            type="button"
            onClick={onReject}
            disabled={busy}
            className="px-3 py-1 text-xs border border-border rounded
                       text-muted-foreground hover:text-danger
                       hover:border-danger/30 disabled:opacity-50"
          >
            Reject
          </button>
          <button
            type="button"
            onClick={onApprove}
            disabled={busy}
            className="px-3 py-1 text-xs bg-success text-white rounded
                       hover:bg-success/90 disabled:opacity-50"
          >
            Approve
          </button>
        </div>
      </div>
      <DataTable
        columns={columns}
        rows={allRaws}
        rowKey={(raw) => raw}
        mobileMode="scroll"
        aria-label={`${strategy.name} calibration curve diff`}
      />
    </div>
  );
}
