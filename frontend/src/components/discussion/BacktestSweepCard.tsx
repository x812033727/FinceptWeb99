import { useState } from "react";
import { ClipboardList } from "lucide-react";
import { SweepAggregateCard } from "./SweepAggregateCard";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { formatTaipei } from "@/lib/timeFormat";
import {
  cancelSweep,
  createSweep,
  deleteSweep,
  fetchSweeps,
  fetchStrategies,
  startSweep,
} from "./_helpers";
import type {
  BacktestSweep,
  BacktestSweepStatus,
} from "./_helpers";
import type { DiscussionMarket } from "@/types/discussion";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import { SweepForm } from "./BacktestSweep/SweepForm";

/**
 * Operator panel for automated multi-day backtest sweeps (PR #274).
 *
 * The operator picks an anchor date + trading-days count + rounds-
 * per-discussion + topic/rules/personas. Backend resolves the
 * actual N trading days from `ohlcv_daily` and runs each one as
 * its own backtest discussion (concluded automatically).
 *
 * The list below the form polls every 5s while any sweep is
 * running so progress bars + status tags update without manual
 * reloads.
 *
 * Concurrency >1 is supported (caps at 3) but the UI nudges
 * operators toward the default 1 — running multiple discussions
 * in parallel saturates LLM provider rate limits and burns daily
 * quota proportionally.
 */

const STATUS_COLORS: Record<BacktestSweepStatus, string> = {
  pending: "bg-secondary/30 text-muted-foreground border-border",
  running: "bg-info/30 text-info border-info/50",
  completed: "bg-success/10 text-success border-success/30",
  cancelled: "bg-warning/10 text-warning border-warning/30",
  failed: "bg-danger/10 text-danger border-danger/30",
};

function SweepProgressRow({
  sweep,
  onStart,
  onCancel,
  onDelete,
}: {
  sweep: BacktestSweep;
  onStart: (id: string) => void;
  onCancel: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  const { t } = useTranslation();
  const [showAggregate, setShowAggregate] = useState(false);
  const total = sweep.resolved_dates.length || sweep.trading_days_count;
  const done = sweep.completed_dates.length;
  const failed = sweep.failed_dates.length;
  const pct = total > 0 ? Math.round(((done + failed) / total) * 100) : 0;
  const isRunning = sweep.status === "running";
  const isPending = sweep.status === "pending";
  const isTerminal =
    sweep.status === "completed" ||
    sweep.status === "cancelled" ||
    sweep.status === "failed";

  return (
    <li className="bg-card border border-border rounded-lg p-3 space-y-1.5 text-xs">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-baseline gap-2 min-w-0">
          <span
            className={`px-1.5 py-0.5 rounded border text-micro uppercase tracking-wider ${STATUS_COLORS[sweep.status]}`}
          >
            {sweep.status}
          </span>
          <span className="font-mono">{sweep.anchor_date}</span>
          <span className="text-muted-foreground">
            ×{sweep.trading_days_count} {t("sweep.days")} ·
            {" "}{sweep.rounds_per_discussion} {t("sweep.rounds")} ·
            {" "}c={sweep.concurrency}
            {sweep.auto_post_mortem !== false ? (
              <span className="ml-1 inline-flex items-center gap-0.5 text-purple-300/80">· <ClipboardList className="h-3 w-3" aria-hidden="true" /></span>
            ) : null}
          </span>
        </div>
        <div className="flex gap-1.5 shrink-0">
          {isPending && (
            <button
              onClick={() => onStart(sweep.id)}
              className="px-2 py-0.5 rounded border border-success/30 text-success hover:bg-success/10"
            >
              {t("sweep.start")}
            </button>
          )}
          {isRunning && (
            <button
              onClick={() => onCancel(sweep.id)}
              className="px-2 py-0.5 rounded border border-warning/30 text-warning hover:bg-warning/10"
            >
              {t("sweep.cancel")}
            </button>
          )}
          {isTerminal && (
            <button
              onClick={() => onDelete(sweep.id)}
              className="px-2 py-0.5 rounded border border-border text-muted-foreground hover:text-danger"
            >
              {t("sweep.delete")}
            </button>
          )}
        </div>
      </div>

      <div className="text-muted-foreground truncate" title={sweep.topic}>
        {sweep.topic}
      </div>

      <div
        className="h-1.5 bg-secondary/40 rounded overflow-hidden"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        title={`${done}/${total} ${t("sweep.completed_label")}`}
      >
        <div
          className="h-full bg-success/70"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex justify-between text-micro text-muted-foreground tabular-nums">
        <span>
          {t("sweep.completed_label")}: {done}/{total}
          {failed > 0 ? (
            <span className="text-danger">
              {" "}· {t("sweep.failed_label")}: {failed}
            </span>
          ) : null}
        </span>
        <span>
          {sweep.cancelled_at
            ? `${t("sweep.cancelled_at")}: ${formatTaipei(sweep.cancelled_at)}`
            : sweep.completed_at
            ? `${t("sweep.completed_at")}: ${formatTaipei(sweep.completed_at)}`
            : sweep.started_at
            ? `${t("sweep.started_at")}: ${formatTaipei(sweep.started_at)}`
            : formatTaipei(sweep.created_at)}
        </span>
      </div>

      {sweep.error_message ? (
        <p className="text-micro text-danger">
          {t("sweep.error_label")}: {sweep.error_message}
        </p>
      ) : null}
      {failed > 0 ? (
        <details className="text-micro text-muted-foreground">
          <summary className="cursor-pointer">
            {t("sweep.failed_dates_summary", { count: failed })}
          </summary>
          <ul className="mt-1 space-y-0.5">
            {sweep.failed_dates.map((fd) => (
              <li key={fd.date} className="font-mono">
                {fd.date} —{" "}
                <span className="text-danger">{fd.error}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {/* PR-B: aggregate panel — lazy-mounted on click so the
          query doesn't fire for every collapsed row. */}
      {done > 0 ? (
        <div className="pt-1.5 border-t border-border/40 space-y-1.5">
          <button
            type="button"
            onClick={() => setShowAggregate((v) => !v)}
            className="text-micro text-primary hover:text-primary/80"
          >
            {showAggregate
              ? t("sweep.hide_aggregate", "▼ 收起聚合儀表板")
              : t("sweep.show_aggregate", "▶ 展開聚合儀表板")}
          </button>
          {showAggregate && (
            <SweepAggregateCard sweepId={sweep.id} />
          )}
        </div>
      ) : null}
    </li>
  );
}

export function BacktestSweepCard({
  topic,
  rules,
  market,
  personaIds,
  forceOpen,
  hideHeader,
}: {
  topic: string;
  rules: string;
  market: DiscussionMarket;
  personaIds: string[];
  /** When true, ignore the persisted collapse state and always
   * render the body. Used by the desktop toolbar popover where
   * the chrome IS the open/close affordance. */
  forceOpen?: boolean;
  /** When true, omit the CollapsibleHeader (the popover trigger
   * already labels itself). */
  hideHeader?: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { open: persistedOpen, toggle } = useCollapsible(
    "discussion.sweep_panel", false,
  );
  const open = forceOpen ?? persistedOpen;

  const strategiesQuery = useQuery({
    queryKey: ["strategy-templates"],
    queryFn: fetchStrategies,
    enabled: open,
  });

  const sweepsQuery = useQuery({
    queryKey: ["backtest-sweeps"],
    queryFn: fetchSweeps,
    enabled: open,
    // Poll every 5s while any sweep is running so the progress
    // bar advances without manual refresh.
    refetchInterval: (q) => {
      const data = q.state.data as BacktestSweep[] | undefined;
      if (!data) return false;
      return data.some((s) => s.status === "running" || s.status === "pending")
        ? 5000
        : false;
    },
  });

  const createMut = useMutation({
    mutationFn: createSweep,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["backtest-sweeps"] });
    },
  });
  const startMut = useMutation({
    mutationFn: startSweep,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["backtest-sweeps"] });
    },
  });
  const cancelMut = useMutation({
    mutationFn: cancelSweep,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["backtest-sweeps"] });
    },
  });
  const deleteMut = useMutation({
    mutationFn: deleteSweep,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["backtest-sweeps"] });
    },
  });

  const sweeps = sweepsQuery.data ?? [];

  return (
    <div className="bg-card border border-border rounded-lg p-3 space-y-3">
      {hideHeader ? null : (
        <CollapsibleHeader
          open={open}
          toggle={toggle}
          title={t("sweep.title")}
          subtitle={t("sweep.subtitle")}
        />
      )}

      {open && (
        <>
          <SweepForm
            topic={topic}
            rules={rules}
            market={market}
            personaIds={personaIds}
            strategies={strategiesQuery.data ?? []}
            onSubmit={(body) => createMut.mutate(body)}
            isSubmitting={createMut.isPending}
          />

          {createMut.error ? (
            <p className="text-meta text-danger">
              {(createMut.error as { response?: { data?: { detail?: string } } })
                ?.response?.data?.detail ??
                (createMut.error as Error).message}
            </p>
          ) : null}

          <div className="space-y-1.5">
            <h5 className="text-label font-semibold text-muted-foreground uppercase tracking-wider">
              {t("sweep.list_title")}
            </h5>
            {sweepsQuery.isLoading ? (
              <p className="text-xs text-muted-foreground animate-pulse">
                {t("common.loading")}
              </p>
            ) : sweeps.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                {t("sweep.empty")}
              </p>
            ) : (
              <ul className="space-y-2">
                {sweeps.map((s) => (
                  <SweepProgressRow
                    key={s.id}
                    sweep={s}
                    onStart={(id) => startMut.mutate(id)}
                    onCancel={(id) => cancelMut.mutate(id)}
                    onDelete={(id) => deleteMut.mutate(id)}
                  />
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  );
}
