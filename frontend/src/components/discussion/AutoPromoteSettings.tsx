/**
 * PR-4b: per-strategy auto-promote settings panel.
 *
 * Inline editor for `auto_promote_enabled` + the two KPI
 * thresholds (min OOS brier improvement, min OOS hit rate). Sits
 * next to the WalkForwardSection on each StrategyTemplateCard
 * row so the operator can opt in / tighten thresholds without
 * leaving the strategy detail.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import type { StrategyTemplate } from "./_helpers";


export function AutoPromoteSettings({
  strategy,
}: {
  strategy: StrategyTemplate;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const [enabled, setEnabled] = useState(
    strategy.auto_promote_enabled ?? false,
  );
  const [minBrierImprovement, setMinBrierImprovement] = useState(
    strategy.auto_promote_min_oos_brier_improvement ?? 0.0,
  );
  const [minHitRate, setMinHitRate] = useState(
    strategy.auto_promote_min_oos_hit_rate ?? 0.5,
  );
  const [showAdvanced, setShowAdvanced] = useState(false);

  const mut = useMutation({
    mutationFn: async () => {
      const { data } = await api.patch(
        `/discussion/strategies/${strategy.id}/auto-promote`,
        {
          enabled,
          min_brier_improvement: minBrierImprovement,
          min_hit_rate: minHitRate,
        },
      );
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategies"] });
    },
  });

  const dirty =
    enabled !== (strategy.auto_promote_enabled ?? false)
    || minBrierImprovement
        !== (strategy.auto_promote_min_oos_brier_improvement ?? 0.0)
    || minHitRate !== (strategy.auto_promote_min_oos_hit_rate ?? 0.5);

  return (
    <div className="border-t border-border/50 mt-2 pt-2 space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <label className="flex items-center gap-2 text-[11px] cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="accent-success"
          />
          <span className="text-foreground font-medium">
            {t("discussion.auto_promote.title")}
          </span>
        </label>
        <button
          type="button"
          onClick={() => setShowAdvanced((v) => !v)}
          className="text-micro text-muted-foreground hover:text-foreground"
        >
          {showAdvanced
            ? t("discussion.auto_promote.hide_advanced")
            : t("discussion.auto_promote.show_advanced")}
        </button>
      </div>
      <p className="text-micro text-muted-foreground leading-tight">
        {t("discussion.auto_promote.subtitle")}
      </p>

      {showAdvanced && (
        <div className="grid grid-cols-2 gap-2 mt-1">
          <label className="flex flex-col gap-0.5 text-micro">
            <span className="text-muted-foreground">
              {t("discussion.auto_promote.min_brier_improvement")}
            </span>
            <input
              type="number"
              min="0"
              max="1"
              step="0.01"
              value={minBrierImprovement}
              onChange={(e) => setMinBrierImprovement(Number(e.target.value))}
              className="bg-background border border-border rounded px-1 py-0.5
                         text-[11px] tabular-nums"
            />
          </label>
          <label className="flex flex-col gap-0.5 text-micro">
            <span className="text-muted-foreground">
              {t("discussion.auto_promote.min_hit_rate")}
            </span>
            <input
              type="number"
              min="0"
              max="1"
              step="0.01"
              value={minHitRate}
              onChange={(e) => setMinHitRate(Number(e.target.value))}
              className="bg-background border border-border rounded px-1 py-0.5
                         text-[11px] tabular-nums"
            />
          </label>
        </div>
      )}

      {dirty && (
        <div className="flex justify-end">
          <button
            type="button"
            onClick={() => mut.mutate()}
            disabled={mut.isPending}
            className="px-2 py-0.5 text-micro rounded bg-success text-white
                       hover:bg-success/90 disabled:opacity-50"
          >
            {mut.isPending
              ? t("common.saving")
              : t("common.save")}
          </button>
        </div>
      )}
    </div>
  );
}
