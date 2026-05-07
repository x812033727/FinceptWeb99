import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import api from "@/lib/api";

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
    <div className="bg-card border border-amber-700/40 rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title="🛡️ Calibration deployment gate"
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
          <div className="text-[11px] text-amber-300 truncate">
            {payload.pending_reason ?? "Pending review"}
          </div>
          {payload.pending_at && (
            <div className="text-[10px] text-muted-foreground">
              Queued: {new Date(payload.pending_at).toLocaleString()}
            </div>
          )}
        </div>
        <div className="flex gap-2 shrink-0">
          <button
            type="button"
            onClick={onReject}
            disabled={busy}
            className="px-3 py-1 text-xs border border-border rounded
                       text-muted-foreground hover:text-red-300
                       hover:border-red-700/60 disabled:opacity-50"
          >
            Reject
          </button>
          <button
            type="button"
            onClick={onApprove}
            disabled={busy}
            className="px-3 py-1 text-xs bg-emerald-700 text-white rounded
                       hover:bg-emerald-600 disabled:opacity-50"
          >
            Approve
          </button>
        </div>
      </div>
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-muted-foreground border-b border-border">
            <th className="text-left py-1">raw</th>
            <th className="text-right py-1">live</th>
            <th className="text-right py-1">pending</th>
            <th className="text-right py-1">Δ</th>
          </tr>
        </thead>
        <tbody>
          {allRaws.map((raw) => {
            const l = liveAt(raw);
            const p = pendingAt(raw);
            const delta = l !== undefined && p !== undefined ? p - l : null;
            return (
              <tr key={raw} className="border-b border-border/40">
                <td className="py-1 font-mono">{raw.toFixed(2)}</td>
                <td className="py-1 text-right font-mono">
                  {l !== undefined ? l.toFixed(3) : "—"}
                </td>
                <td className="py-1 text-right font-mono">
                  {p !== undefined ? p.toFixed(3) : "—"}
                </td>
                <td
                  className={`py-1 text-right font-mono ${
                    delta === null
                      ? "text-muted-foreground"
                      : delta > 0
                        ? "text-emerald-300"
                        : delta < 0
                          ? "text-red-300"
                          : ""
                  }`}
                >
                  {delta === null
                    ? "—"
                    : `${delta > 0 ? "+" : ""}${delta.toFixed(3)}`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
