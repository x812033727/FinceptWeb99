import { useState } from "react";
import { SweepAggregateCard } from "./SweepAggregateCard";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  CreateBacktestSweepInput,
  StrategyTemplate,
} from "./_helpers";
import type { DiscussionMarket } from "@/types/discussion";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";

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
  running: "bg-blue-900/30 text-blue-300 border-blue-800/50",
  completed: "bg-emerald-900/30 text-emerald-300 border-emerald-800/50",
  cancelled: "bg-amber-900/30 text-amber-300 border-amber-800/50",
  failed: "bg-red-900/30 text-red-300 border-red-800/50",
};

interface SweepFormProps {
  topic: string;
  rules: string;
  market: DiscussionMarket;
  personaIds: string[];
  strategies: StrategyTemplate[];
  onSubmit: (body: CreateBacktestSweepInput) => void;
  isSubmitting: boolean;
}

function SweepForm({
  topic, rules, market, personaIds, strategies, onSubmit, isSubmitting,
}: SweepFormProps) {
  const { t } = useTranslation();
  const today = new Date().toISOString().slice(0, 10);
  const [anchorDate, setAnchorDate] = useState<string>(today);
  const [tradingDays, setTradingDays] = useState<number>(5);
  const [roundsPerDiscussion, setRoundsPerDiscussion] = useState<number>(1);
  const [concurrency, setConcurrency] = useState<number>(1);
  const [autoPostMortem, setAutoPostMortem] = useState<boolean>(true);
  // PR-A: when chosen, server back-fills topic/rules/personas/etc
  // from the template. Caller-supplied fields still win, so the
  // form's runtime knobs (rounds/concurrency/auto_post_mortem) are
  // sent inline to give the operator a per-launch override.
  const [strategyId, setStrategyId] = useState<string>("");

  function submit() {
    if (!anchorDate) return;
    if (!strategyId && personaIds.length === 0) return;
    const body: CreateBacktestSweepInput = {
      anchor_date: anchorDate,
      trading_days_count: tradingDays,
      rounds_per_discussion: roundsPerDiscussion,
      concurrency,
      auto_post_mortem: autoPostMortem,
    };
    if (strategyId) {
      body.strategy_id = strategyId;
    } else {
      body.topic = topic;
      body.rules = rules;
      body.market = market;
      body.persona_ids = personaIds;
    }
    onSubmit(body);
  }

  return (
    <div className="bg-secondary/20 border border-border rounded-lg p-3 space-y-2">
      <p className="text-[11px] text-muted-foreground leading-relaxed">
        {t("sweep.intro")}
      </p>
      {strategies.length > 0 && (
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-muted-foreground">
            {t("sweep.strategy_picker", "從策略模板載入（可選）")}
          </span>
          <select
            value={strategyId}
            onChange={(e) => setStrategyId(e.target.value)}
            className="bg-background border border-border rounded px-2 py-1"
          >
            <option value="">
              {t("sweep.strategy_inline",
                 "（不使用模板，套用上方主討論的題目+規則）")}
            </option>
            {strategies.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} · {s.market} · {s.persona_ids.length}
                {t("strategy.personas", "專家")}
              </option>
            ))}
          </select>
        </label>
      )}
      <div className="grid grid-cols-2 gap-2 text-xs">
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">{t("sweep.anchor_date")}</span>
          <input
            type="date"
            value={anchorDate}
            onChange={(e) => setAnchorDate(e.target.value)}
            className="bg-background border border-border rounded px-2 py-1 text-xs"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            {t("sweep.trading_days_count")}（1-60）
          </span>
          <input
            type="number"
            min={1}
            max={60}
            value={tradingDays}
            onChange={(e) =>
              setTradingDays(
                Math.max(1, Math.min(60, Number(e.target.value) || 1)),
              )
            }
            className="bg-background border border-border rounded px-2 py-1 text-xs"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            {t("sweep.rounds_per_discussion")}（1-5）
          </span>
          <input
            type="number"
            min={1}
            max={5}
            value={roundsPerDiscussion}
            onChange={(e) =>
              setRoundsPerDiscussion(
                Math.max(1, Math.min(5, Number(e.target.value) || 1)),
              )
            }
            className="bg-background border border-border rounded px-2 py-1 text-xs"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            {t("sweep.concurrency")}（1-3，預設 1）
          </span>
          <input
            type="number"
            min={1}
            max={3}
            value={concurrency}
            onChange={(e) =>
              setConcurrency(
                Math.max(1, Math.min(3, Number(e.target.value) || 1)),
              )
            }
            className="bg-background border border-border rounded px-2 py-1 text-xs"
          />
        </label>
      </div>
      <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
        <input
          type="checkbox"
          checked={autoPostMortem}
          onChange={(e) => setAutoPostMortem(e.target.checked)}
          className="rounded border-border"
        />
        <span className="text-muted-foreground">
          {t("sweep.auto_post_mortem_label")}
        </span>
      </label>
      <p className="text-[10px] text-muted-foreground/70 leading-relaxed">
        {autoPostMortem
          ? t("sweep.auto_post_mortem_on_help")
          : t("sweep.auto_post_mortem_off_help")}
      </p>
      <p className="text-[10px] text-muted-foreground/70 leading-relaxed">
        {t("sweep.reuse_note", {
          personas: personaIds.length,
        })}
      </p>
      <button
        type="button"
        onClick={submit}
        disabled={
          isSubmitting ||
          !anchorDate ||
          personaIds.length === 0 ||
          !topic.trim() ||
          !rules.trim()
        }
        className="w-full px-3 py-1.5 rounded-md border border-purple-800/50 text-purple-300 text-xs hover:bg-purple-900/20 transition-colors disabled:opacity-50"
      >
        {isSubmitting ? t("sweep.creating") : t("sweep.create")}
      </button>
    </div>
  );
}

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
            className={`px-1.5 py-0.5 rounded border text-[10px] uppercase tracking-wider ${STATUS_COLORS[sweep.status]}`}
          >
            {sweep.status}
          </span>
          <span className="font-mono">{sweep.anchor_date}</span>
          <span className="text-muted-foreground">
            ×{sweep.trading_days_count} {t("sweep.days")} ·
            {" "}{sweep.rounds_per_discussion} {t("sweep.rounds")} ·
            {" "}c={sweep.concurrency}
            {sweep.auto_post_mortem !== false ? (
              <span className="ml-1 text-purple-300/80">· 📋</span>
            ) : null}
          </span>
        </div>
        <div className="flex gap-1.5 shrink-0">
          {isPending && (
            <button
              onClick={() => onStart(sweep.id)}
              className="px-2 py-0.5 rounded border border-emerald-800/50 text-emerald-300 hover:bg-emerald-900/20"
            >
              {t("sweep.start")}
            </button>
          )}
          {isRunning && (
            <button
              onClick={() => onCancel(sweep.id)}
              className="px-2 py-0.5 rounded border border-amber-800/50 text-amber-300 hover:bg-amber-900/20"
            >
              {t("sweep.cancel")}
            </button>
          )}
          {isTerminal && (
            <button
              onClick={() => onDelete(sweep.id)}
              className="px-2 py-0.5 rounded border border-border text-muted-foreground hover:text-red-400"
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
          className="h-full bg-emerald-500/70"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex justify-between text-[10px] text-muted-foreground tabular-nums">
        <span>
          {t("sweep.completed_label")}: {done}/{total}
          {failed > 0 ? (
            <span className="text-red-400">
              {" "}· {t("sweep.failed_label")}: {failed}
            </span>
          ) : null}
        </span>
        <span>
          {sweep.cancelled_at
            ? `${t("sweep.cancelled_at")}: ${new Date(sweep.cancelled_at).toLocaleString()}`
            : sweep.completed_at
            ? `${t("sweep.completed_at")}: ${new Date(sweep.completed_at).toLocaleString()}`
            : sweep.started_at
            ? `${t("sweep.started_at")}: ${new Date(sweep.started_at).toLocaleString()}`
            : new Date(sweep.created_at).toLocaleString()}
        </span>
      </div>

      {sweep.error_message ? (
        <p className="text-[10px] text-red-300">
          {t("sweep.error_label")}: {sweep.error_message}
        </p>
      ) : null}
      {failed > 0 ? (
        <details className="text-[10px] text-muted-foreground">
          <summary className="cursor-pointer">
            {t("sweep.failed_dates_summary", { count: failed })}
          </summary>
          <ul className="mt-1 space-y-0.5">
            {sweep.failed_dates.map((fd) => (
              <li key={fd.date} className="font-mono">
                {fd.date} —{" "}
                <span className="text-red-300">{fd.error}</span>
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
            className="text-[10px] text-blue-300 hover:text-blue-200"
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
}: {
  topic: string;
  rules: string;
  market: DiscussionMarket;
  personaIds: string[];
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { open, toggle } = useCollapsible("discussion.sweep_panel", false);

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
      <CollapsibleHeader
        open={open}
        toggle={toggle}
        title={t("sweep.title")}
        subtitle={t("sweep.subtitle")}
      />

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
            <p className="text-[11px] text-red-300">
              {(createMut.error as { response?: { data?: { detail?: string } } })
                ?.response?.data?.detail ??
                (createMut.error as Error).message}
            </p>
          ) : null}

          <div className="space-y-1.5">
            <h5 className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider">
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
