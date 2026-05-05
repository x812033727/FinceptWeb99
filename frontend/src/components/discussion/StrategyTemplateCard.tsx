import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createStrategy,
  deleteStrategy,
  fetchStrategies,
  learnStrategyWeights,
  type CreateStrategyInput,
  type PersonaWeightLearnResult,
  type StrategyTemplate,
} from "./_helpers";
import type { DiscussionMarket } from "@/types/discussion";
import { useCollapsible, CollapsibleHeader } from "@/components/Collapsible";
import { SweepAggregateCard } from "./SweepAggregateCard";

/** Strategy templates (PR-A).
 *
 * Operators can save the current sweep recipe (topic + rules +
 * persona roster + default knobs) as a reusable template. The
 * BacktestSweepCard's strategy picker reads this list to offer a
 * one-click "load + go" sweep. PR-C's weight learner writes
 * `persona_weights` here; this card surfaces the per-persona
 * weight badges so the operator can see what was learned. */

interface StrategyFormState {
  name: string;
  description: string;
  topic: string;
  rules: string;
  market: DiscussionMarket;
  personaIdsCsv: string;
  defaultRounds: number;
  defaultConcurrency: number;
  defaultAutoPostMortem: boolean;
}

const DEFAULT_FORM: StrategyFormState = {
  name: "",
  description: "",
  topic: "",
  rules: "",
  market: "TW",
  personaIdsCsv: "",
  defaultRounds: 1,
  defaultConcurrency: 1,
  defaultAutoPostMortem: true,
};

export function StrategyTemplateCard({
  prefill,
}: {
  /** Optional pre-fill from the parent BacktestSweepCard's
   * current form so the operator can save what they're already
   * looking at without retyping. */
  prefill?: Partial<StrategyFormState>;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { open, toggle } = useCollapsible("discussion.strategy_panel", false);
  const [form, setForm] = useState<StrategyFormState>(() => ({
    ...DEFAULT_FORM,
    ...(prefill ?? {}),
  }));
  const [showForm, setShowForm] = useState(false);

  const query = useQuery({
    queryKey: ["strategy-templates"],
    queryFn: fetchStrategies,
    enabled: open,
  });

  const createMut = useMutation({
    mutationFn: createStrategy,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategy-templates"] });
      setShowForm(false);
      setForm({ ...DEFAULT_FORM, ...(prefill ?? {}) });
    },
  });

  const deleteMut = useMutation({
    mutationFn: deleteStrategy,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategy-templates"] });
    },
  });

  const strategies = query.data ?? [];

  function submit() {
    const personaIds = form.personaIdsCsv
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (!form.name.trim() || !form.topic.trim() || !form.rules.trim()
        || personaIds.length === 0) {
      return;
    }
    const body: CreateStrategyInput = {
      name: form.name.trim(),
      description: form.description.trim() || null,
      topic: form.topic.trim(),
      rules: form.rules.trim(),
      market: form.market,
      persona_ids: personaIds,
      default_rounds: form.defaultRounds,
      default_concurrency: form.defaultConcurrency,
      default_auto_post_mortem: form.defaultAutoPostMortem,
    };
    createMut.mutate(body);
  }

  return (
    <div className="bg-card border border-border rounded-lg p-3 space-y-3">
      <CollapsibleHeader
        open={open}
        toggle={toggle}
        title={t("strategy.title", "策略模板")}
        subtitle={t(
          "strategy.subtitle",
          "封裝題目+規則+專家組合，回測掃描時一鍵套用",
        )}
      />

      {open && (
        <>
          <div className="flex items-center justify-between">
            <h5 className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider">
              {t("strategy.list_title", "我的策略")}（{strategies.length}）
            </h5>
            <button
              type="button"
              onClick={() => setShowForm((v) => !v)}
              className="text-[11px] text-blue-300 hover:text-blue-200"
            >
              {showForm
                ? t("common.cancel", "取消")
                : t("strategy.new", "+ 新策略")}
            </button>
          </div>

          {showForm && (
            <StrategyFormBlock
              form={form}
              setForm={setForm}
              onSubmit={submit}
              isSubmitting={createMut.isPending}
              error={createMut.error}
            />
          )}

          <div className="space-y-2">
            {query.isLoading ? (
              <p className="text-xs text-muted-foreground animate-pulse">
                {t("common.loading")}
              </p>
            ) : strategies.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                {t("strategy.empty", "目前沒有策略，新增一個試試")}
              </p>
            ) : (
              <ul className="space-y-1.5">
                {strategies.map((s) => (
                  <StrategyRow
                    key={s.id}
                    strategy={s}
                    onDelete={() => deleteMut.mutate(s.id)}
                    isDeleting={deleteMut.isPending}
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

function StrategyFormBlock({
  form, setForm, onSubmit, isSubmitting, error,
}: {
  form: StrategyFormState;
  setForm: (f: StrategyFormState) => void;
  onSubmit: () => void;
  isSubmitting: boolean;
  error: unknown;
}) {
  const { t } = useTranslation();
  return (
    <div className="bg-secondary/20 border border-border rounded-lg p-3 space-y-2 text-xs">
      <label className="flex flex-col gap-1">
        <span className="text-muted-foreground">
          {t("strategy.name", "策略名稱")}
        </span>
        <input
          value={form.name}
          maxLength={120}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          className="bg-background border border-border rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-muted-foreground">
          {t("strategy.description", "描述（選填）")}
        </span>
        <input
          value={form.description}
          onChange={(e) => setForm({ ...form, description: e.target.value })}
          className="bg-background border border-border rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-muted-foreground">
          {t("strategy.topic", "題目")}
        </span>
        <textarea
          rows={2}
          value={form.topic}
          onChange={(e) => setForm({ ...form, topic: e.target.value })}
          className="bg-background border border-border rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-muted-foreground">
          {t("strategy.rules", "規則")}
        </span>
        <textarea
          rows={2}
          value={form.rules}
          onChange={(e) => setForm({ ...form, rules: e.target.value })}
          className="bg-background border border-border rounded px-2 py-1"
        />
      </label>
      <div className="grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            {t("strategy.market", "市場")}
          </span>
          <select
            value={form.market}
            onChange={(e) =>
              setForm({ ...form, market: e.target.value as DiscussionMarket })
            }
            className="bg-background border border-border rounded px-2 py-1"
          >
            <option value="TW">TW</option>
            <option value="US">US</option>
            <option value="GLOBAL">GLOBAL</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            {t("strategy.persona_ids", "專家 IDs（逗號分隔）")}
          </span>
          <input
            value={form.personaIdsCsv}
            onChange={(e) => setForm({ ...form, personaIdsCsv: e.target.value })}
            placeholder="bull, bear, quant"
            className="bg-background border border-border rounded px-2 py-1"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            {t("strategy.default_rounds", "預設輪數")}
          </span>
          <input
            type="number" min={1} max={5}
            value={form.defaultRounds}
            onChange={(e) =>
              setForm({ ...form, defaultRounds: Number(e.target.value) })
            }
            className="bg-background border border-border rounded px-2 py-1"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            {t("strategy.default_concurrency", "預設並行")}
          </span>
          <input
            type="number" min={1} max={3}
            value={form.defaultConcurrency}
            onChange={(e) =>
              setForm({ ...form, defaultConcurrency: Number(e.target.value) })
            }
            className="bg-background border border-border rounded px-2 py-1"
          />
        </label>
      </div>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={form.defaultAutoPostMortem}
          onChange={(e) =>
            setForm({ ...form, defaultAutoPostMortem: e.target.checked })
          }
        />
        <span className="text-muted-foreground">
          {t("strategy.default_auto_post_mortem", "預設啟用事後檢討")}
        </span>
      </label>
      {error ? (
        <p className="text-[11px] text-red-300">
          {(error as { response?: { data?: { detail?: string } } })
            ?.response?.data?.detail ?? (error as Error).message}
        </p>
      ) : null}
      <button
        type="button"
        onClick={onSubmit}
        disabled={isSubmitting}
        className="bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold px-3 py-1.5 rounded disabled:opacity-50"
      >
        {isSubmitting
          ? t("common.saving", "儲存中…")
          : t("strategy.save", "儲存策略")}
      </button>
    </div>
  );
}

function StrategyRow({
  strategy, onDelete, isDeleting,
}: {
  strategy: StrategyTemplate;
  onDelete: () => void;
  isDeleting: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [showAggregate, setShowAggregate] = useState(false);
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
            <span className="ml-2 text-[10px] text-muted-foreground">
              {strategy.market} · {strategy.persona_ids.length}{" "}
              {t("strategy.personas", "專家")} · r={strategy.default_rounds}
              · c={strategy.default_concurrency}
            </span>
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
          className="text-[11px] text-red-300 hover:text-red-200 disabled:opacity-50 shrink-0"
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
                className="text-[10px] bg-emerald-900/30 text-emerald-300 border border-emerald-800/50 rounded px-1.5 py-0.5 font-mono"
                title={t("strategy.weight_tooltip", "PR-C 學到的權重")}
              >
                {pid}: {w.toFixed(2)}
              </span>
            ))}
        </div>
      ) : null}
      <div className="flex items-center gap-3 text-[10px]">
        <button
          type="button"
          onClick={() => setShowAggregate((v) => !v)}
          className="text-blue-300 hover:text-blue-200"
        >
          {showAggregate
            ? t("strategy.hide_aggregate", "▼ 收起跨 sweep 績效")
            : t("strategy.show_aggregate", "▶ 展開跨 sweep 績效")}
        </button>
        <button
          type="button"
          onClick={() => learnMut.mutate()}
          disabled={learnMut.isPending}
          className="text-emerald-300 hover:text-emerald-200 disabled:opacity-50"
          title={t("strategy.learn_tooltip",
                   "依過往 sweep 命中率重算 persona 權重")}
        >
          {learnMut.isPending
            ? t("strategy.learning", "學習中…")
            : t("strategy.learn", "🧠 重新學習權重")}
        </button>
        {strategy.weights_updated_at ? (
          <span className="text-muted-foreground" title={strategy.weights_updated_at}>
            {t("strategy.last_learned", "上次學習")}：
            {new Date(strategy.weights_updated_at).toLocaleDateString()}
          </span>
        ) : null}
      </div>
      {learnResult ? (
        <p className={
          learnResult.updated
            ? "text-[10px] text-emerald-300"
            : "text-[10px] text-amber-300"
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
        <p className="text-[10px] text-red-300">
          {(learnMut.error as { response?: { data?: { detail?: string } } })
            ?.response?.data?.detail ?? (learnMut.error as Error).message}
        </p>
      ) : null}
      {showAggregate && (
        <SweepAggregateCard strategyId={strategy.id} />
      )}
    </li>
  );
}
