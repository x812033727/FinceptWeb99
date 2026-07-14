import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Brain, Timer, TrendingDown } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { formatTaipei } from "@/lib/timeFormat";
import {
  learnStrategyWeights,
  type PersonaWeightLearnResult,
  type StrategyTemplate,
} from "../_helpers";
import { AutoPromoteSettings } from "../AutoPromoteSettings";
import { BrierTrendChart } from "../BrierTrendChart";
import { HealthMetricsSparkline } from "../HealthMetricsSparkline";
import { MaturityBadge } from "../MaturityBadge";
import { PersonaLeaderboardCard } from "../PersonaLeaderboardCard";
import { PersonaStatusGrid } from "../PersonaStatusGrid";
import { StrategyTimelineCard } from "../StrategyTimelineCard";
import { SweepAggregateCard } from "../SweepAggregateCard";
import { VersionHistorySection } from "../VersionHistorySection";
import { WalkForwardSection } from "./walkforward";

export function StrategyRow({
  strategy, onDelete, isDeleting,
}: {
  strategy: StrategyTemplate;
  onDelete: () => void;
  isDeleting: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [showAggregate, setShowAggregate] = useState(false);
  const [showBrierTrend, setShowBrierTrend] = useState(false);
  const learnMut = useMutation({
    mutationFn: () => learnStrategyWeights(strategy.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategy-templates"] });
    },
  });
  const learnResult = learnMut.data as PersonaWeightLearnResult | undefined;
  const weightEntries = useMemo(
    () => Object.entries(strategy.persona_weights ?? {}),
    [strategy.persona_weights],
  );
  return (
    <li className="bg-secondary/20 border border-border rounded p-2 space-y-1">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs font-semibold text-foreground truncate">
            {strategy.name}
            <span className="ml-2 text-micro text-muted-foreground">
              {strategy.market} · {strategy.persona_ids.length}{" "}
              {t("strategy.personas", "專家")} · r={strategy.default_rounds}
              · c={strategy.default_concurrency}
            </span>
            {strategy.auto_schedule_enabled ? (
              <span
                className="ml-2 inline-flex items-center gap-1 text-micro bg-success/15 text-success border border-success/30 rounded px-1 py-0.5"
                title={t(
                  "strategy.auto_schedule_active_tip",
                  "每 {{h}}h 自動掃 {{n}} 日（offset {{off}}）",
                  {
                    h: strategy.auto_schedule_cadence_hours,
                    n: strategy.auto_schedule_trading_days_count,
                    off: strategy.auto_schedule_anchor_offset_days,
                  },
                )}
              >
                <Timer className="h-3 w-3" /> auto
              </span>
            ) : null}
            {/* PR-4a: lifecycle tier badge — coloured by where the
                strategy sits in cold_start → learning → mature →
                drifting / stale. The tooltip carries the inputs
                (sweep count, sample count, brier ratio) so the
                operator sees WHY without clicking. */}
            {strategy.maturity_tier ? (
              <span className="ml-2 inline-block">
                <MaturityBadge tier={strategy.maturity_tier} />
              </span>
            ) : null}
          </p>
          {strategy.description ? (
            <p className="text-[11px] text-muted-foreground truncate">
              {strategy.description}
            </p>
          ) : null}
        </div>
        <button
          type="button"
          onClick={onDelete}
          disabled={isDeleting}
          className="text-[11px] text-danger hover:text-danger/80 disabled:opacity-50 shrink-0"
        >
          {t("common.delete", "刪除")}
        </button>
      </div>
      {weightEntries.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {weightEntries
            .sort((a, b) => b[1] - a[1])
            .map(([pid, w]) => (
              <span
                key={pid}
                className="text-micro bg-success/10 text-success border border-success/30 rounded px-1.5 py-0.5 font-mono"
                title={t("strategy.weight_tooltip", "PR-C 學到的權重")}
              >
                {pid}: {w.toFixed(2)}
              </span>
            ))}
        </div>
      ) : null}
      <div className="flex items-center gap-3 text-micro">
        <button
          type="button"
          onClick={() => setShowAggregate((v) => !v)}
          className="text-primary hover:text-primary/80"
        >
          {showAggregate
            ? t("strategy.hide_aggregate", "▼ 收起跨 sweep 績效")
            : t("strategy.show_aggregate", "▶ 展開跨 sweep 績效")}
        </button>
        <button
          type="button"
          onClick={() => setShowBrierTrend((v) => !v)}
          className="text-success hover:text-success/80"
          title={t(
            "strategy.brier_trend_button_tip",
            "每個已完成 sweep 一個點;raw vs calibrated Brier 趨勢線",
          )}
        >
          {showBrierTrend
            ? t("strategy.brier_trend_hide", "▼ 收起 Brier 趨勢")
            : (
              <span className="inline-flex items-center gap-1">
                <TrendingDown className="h-3 w-3" />
                {t("strategy.brier_trend_show", "Brier 趨勢")}
              </span>
            )}
        </button>
        <button
          type="button"
          onClick={() => learnMut.mutate()}
          disabled={learnMut.isPending}
          className="text-success hover:text-success/80 disabled:opacity-50"
          title={t("strategy.learn_tooltip",
                   "依過往 sweep 命中率重算 persona 權重")}
        >
          {learnMut.isPending
            ? t("strategy.learning", "學習中…")
            : (
              <span className="inline-flex items-center gap-1">
                <Brain className="h-3 w-3" />
                {t("strategy.learn", "重新學習權重")}
              </span>
            )}
        </button>
        {strategy.weights_updated_at ? (
          <span className="text-muted-foreground" title={strategy.weights_updated_at}>
            {t("strategy.last_learned", "上次學習")}：
            {formatTaipei(strategy.weights_updated_at, "date")}
          </span>
        ) : null}
      </div>
      {learnResult ? (
        <p className={
          learnResult.updated
            ? "text-micro text-success"
            : "text-micro text-warning"
        }>
          {learnResult.updated
            ? t("strategy.learn_done",
                "✔ 已更新 {{n}} 位專家的權重",
                { n: Object.keys(learnResult.weights).length })
            : t("strategy.learn_skipped", "⚠ 跳過：{{r}}",
                { r: learnResult.reason ?? "" })}
        </p>
      ) : null}
      {learnMut.error ? (
        <p className="text-micro text-danger">
          {(learnMut.error as { response?: { data?: { detail?: string } } })
            ?.response?.data?.detail ?? (learnMut.error as Error).message}
        </p>
      ) : null}
      <WalkForwardSection strategy={strategy} />
      <AutoPromoteSettings strategy={strategy} />
      <PersonaStatusGrid strategy={strategy} />
      <HealthMetricsSparkline strategyId={strategy.id} />
      <StrategyTimelineCard
        strategyId={strategy.id}
        market={strategy.market}
      />
      <PersonaLeaderboardCard strategyId={strategy.id} />
      <VersionHistorySection strategyId={strategy.id} />
      {showAggregate && (
        <SweepAggregateCard strategyId={strategy.id} />
      )}
      {showBrierTrend && (
        <BrierTrendChart strategyId={strategy.id} />
      )}
    </li>
  );
}
