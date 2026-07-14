import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { DiscussionMarket } from "@/types/discussion";
import type {
  CreateBacktestSweepInput,
  StrategyTemplate,
} from "../_helpers";

// Pure-move split out of BacktestSweepCard (W-final G8): the launch form is
// self-contained and props-driven, so extracting it keeps the parent card
// under the 500-line threshold with zero behaviour change.

export interface SweepFormProps {
  topic: string;
  rules: string;
  market: DiscussionMarket;
  personaIds: string[];
  strategies: StrategyTemplate[];
  onSubmit: (body: CreateBacktestSweepInput) => void;
  isSubmitting: boolean;
}

export function SweepForm({
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
      <p className="text-micro text-muted-foreground/70 leading-relaxed">
        {autoPostMortem
          ? t("sweep.auto_post_mortem_on_help")
          : t("sweep.auto_post_mortem_off_help")}
      </p>
      <p className="text-micro text-muted-foreground/70 leading-relaxed">
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
