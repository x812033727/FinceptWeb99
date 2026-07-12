/**
 * Side-by-side train vs test fold KPI comparison (PR-A1 + PR-C1
 * follow-up). Renders only when the current sweep is a `test` fold
 * with a paired `parent_sweep_id`. Click to expand → fetches the
 * train sibling via `fetchSweepAggregate` and renders the two side
 * by side so the operator can see how much of the train fold's
 * apparent skill survived the OOS check.
 *
 * The interesting cells:
 *   • Brier raw / calibrated — train < test typically (in-sample
 *     fits noise the test fold can't reproduce). Big gap = strong
 *     overfitting evidence.
 *   • Win rate — same direction. A train fold winning 80% but the
 *     test sibling winning 40% means the persona weights learned
 *     from train are picking out coincidences, not signal.
 *   • Avg PnL D5 — same.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { GraduationCap, FlaskConical, Search } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { fetchSweepAggregate, type SweepAggregate } from "../_helpers";

export function WalkForwardCompareSection({ testAgg }: { testAgg: SweepAggregate }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  if (
    testAgg.fold_kind !== "test"
    || !testAgg.parent_sweep_id
  ) {
    return null;
  }
  return (
    <div className="bg-secondary/20 border border-border rounded p-2 space-y-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 text-[11px] text-purple-300 hover:text-purple-200"
      >
        {open ? (
          t("aggregate.compare_hide", "▼ 收起 train 折比較")
        ) : (
          <>
            <Search className="h-3 w-3" aria-hidden="true" />
            {t(
              "aggregate.compare_show",
              "展開 train vs test 並排 KPI(in-sample vs OOS)",
            )}
          </>
        )}
      </button>
      {open ? (
        <CompareGrid parentSweepId={testAgg.parent_sweep_id} testAgg={testAgg} />
      ) : null}
    </div>
  );
}

function CompareGrid({
  parentSweepId, testAgg,
}: { parentSweepId: string; testAgg: SweepAggregate }) {
  const { t } = useTranslation();
  const { data: trainAgg, isLoading, error } = useQuery({
    queryKey: ["sweep-aggregate", parentSweepId],
    queryFn: () => fetchSweepAggregate(parentSweepId),
  });
  if (isLoading) {
    return (
      <p className="text-[11px] text-muted-foreground animate-pulse">
        {t("common.loading")}
      </p>
    );
  }
  if (error || !trainAgg) {
    return (
      <p className="text-[11px] text-danger">
        {(error as Error | undefined)?.message
          ?? t(
              "aggregate.compare_train_missing",
              "找不到對應的 train 折(可能已被刪除)",
            )}
      </p>
    );
  }
  return (
    <div className="space-y-1">
      <div className="grid grid-cols-3 gap-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        <div>{t("aggregate.compare_metric", "指標")}</div>
        <div className="text-center">
          <span className="inline-flex items-center justify-center gap-1">
            <GraduationCap className="h-3 w-3" aria-hidden="true" />
            {t("aggregate.compare_train", "Train (in-sample)")}
          </span>
        </div>
        <div className="text-center">
          <span className="inline-flex items-center justify-center gap-1">
            <FlaskConical className="h-3 w-3" aria-hidden="true" />
            {t("aggregate.compare_test", "Test (OOS)")}
          </span>
        </div>
      </div>
      <CompareRow
        label={t("aggregate.brier_raw", "Raw Brier")}
        train={trainAgg.brier_score ?? null}
        test={testAgg.brier_score ?? null}
        // Lower is better for Brier — flip the comparison logic.
        lowerIsBetter
      />
      <CompareRow
        label={t("aggregate.brier_calibrated", "Calibrated Brier")}
        train={trainAgg.calibrated_brier_score ?? null}
        test={testAgg.calibrated_brier_score ?? null}
        lowerIsBetter
      />
      <CompareRow
        label={t("aggregate.win_rate", "勝率")}
        train={trainAgg.win_rate}
        test={testAgg.win_rate}
        format="percent"
      />
      <CompareRow
        label={t("aggregate.avg_d5_pnl", "D5 平均報酬")}
        train={trainAgg.avg_pnl_pct[4] ?? null}
        test={testAgg.avg_pnl_pct[4] ?? null}
        format="signed_percent"
      />
      <p className="text-[10px] text-muted-foreground mt-1">
        {t(
          "aggregate.compare_overfitting_tip",
          "Train < Test 差距愈大 = 模型在 train 折擬合了雜訊。" +
          "如果 Test 比 Train 差很多,該 strategy 的 persona 權重學習可能過擬合,考慮改 train_window 大小或縮減 fold 數。",
        )}
      </p>
    </div>
  );
}

function CompareRow({
  label, train, test, lowerIsBetter, format,
}: {
  label: string;
  train: number | null;
  test: number | null;
  lowerIsBetter?: boolean;
  format?: "percent" | "signed_percent";
}) {
  const fmt = (n: number | null): string => {
    if (n === null || n === undefined) return "—";
    if (format === "percent") return `${(n * 100).toFixed(0)}%`;
    if (format === "signed_percent") {
      const pct = n * 100;
      const sign = pct > 0 ? "+" : "";
      return `${sign}${pct.toFixed(2)}%`;
    }
    return n.toFixed(3);
  };
  // Highlight the test column when its value is meaningfully
  // worse than train. "Worse" depends on direction — lower
  // Brier is better, higher win_rate / pnl is better.
  let testCls = "text-foreground";
  if (train !== null && test !== null) {
    const epsilon = 0.005;   // 0.5pp / 0.005 brier delta
    const testWorse = lowerIsBetter
      ? test > train + epsilon
      : test < train - epsilon;
    const testBetter = lowerIsBetter
      ? test < train - epsilon
      : test > train + epsilon;
    if (testWorse) testCls = "text-danger";
    else if (testBetter) testCls = "text-success";
  }
  return (
    <div className="grid grid-cols-3 gap-1 text-[11px] font-mono">
      <div className="text-muted-foreground">{label}</div>
      <div className="text-center">{fmt(train)}</div>
      <div className={`text-center ${testCls}`}>{fmt(test)}</div>
    </div>
  );
}
