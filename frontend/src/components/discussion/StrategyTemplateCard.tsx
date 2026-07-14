import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createStrategy,
  deleteStrategy,
  fetchStrategies,
  type CreateStrategyInput,
} from "./_helpers";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import { StrategyFormBlock } from "./StrategyTemplate/form";
import { StrategyRow } from "./StrategyTemplate/row";
import type { StrategyFormState } from "./StrategyTemplate/shared";

/** Strategy templates (PR-A).
 *
 * Operators can save the current sweep recipe (topic + rules +
 * persona roster + default knobs) as a reusable template. The
 * BacktestSweepCard's strategy picker reads this list to offer a
 * one-click "load + go" sweep. PR-C's weight learner writes
 * `persona_weights` here; this card surfaces the per-persona
 * weight badges so the operator can see what was learned. */

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
  autoScheduleEnabled: false,
  autoScheduleCadenceHours: 24,
  autoScheduleAnchorOffsetDays: -1,
  autoScheduleTradingDaysCount: 1,
};

export function StrategyTemplateCard({
  prefill,
  forceOpen,
  hideHeader,
}: {
  /** Optional pre-fill from the parent BacktestSweepCard's
   * current form so the operator can save what they're already
   * looking at without retyping. */
  prefill?: Partial<StrategyFormState>;
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
    "discussion.strategy_panel", false,
  );
  const open = forceOpen ?? persistedOpen;
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
      auto_schedule_enabled: form.autoScheduleEnabled,
      auto_schedule_cadence_hours: form.autoScheduleCadenceHours,
      auto_schedule_anchor_offset_days: form.autoScheduleAnchorOffsetDays,
      auto_schedule_trading_days_count: form.autoScheduleTradingDaysCount,
    };
    createMut.mutate(body);
  }

  return (
    <div className="bg-card border border-border rounded-lg p-3 space-y-3">
      {hideHeader ? null : (
        <CollapsibleHeader
          open={open}
          toggle={toggle}
          title={t("strategy.title", "策略模板")}
          subtitle={t(
            "strategy.subtitle",
            "封裝題目+規則+專家組合，回測掃描時一鍵套用",
          )}
        />
      )}

      {open && (
        <>
          <div className="flex items-center justify-between">
            <h5 className="text-label font-semibold text-muted-foreground uppercase tracking-wider">
              {t("strategy.list_title", "我的策略")}（{strategies.length}）
            </h5>
            <button
              type="button"
              onClick={() => setShowForm((v) => !v)}
              className="text-[11px] text-primary hover:text-primary/80"
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
