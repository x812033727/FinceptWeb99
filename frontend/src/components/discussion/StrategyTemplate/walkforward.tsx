import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Microscope, Rocket } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  triggerWalkForward,
  type StrategyTemplate,
  type WalkForwardPlanResponse,
} from "../_helpers";

/**
 * Walk-forward orchestrator trigger UI (PR-A1 follow-up).
 *
 * The backend orchestrator at POST /api/discussion/strategies/{id}/
 * walk-forward has been live since #341, but until now operators had
 * no way to trigger it from the UI — they had to drop into a Python
 * REPL or curl. This section exposes a small inline form for the
 * three knobs that matter (anchor_date / n_folds / train+test window
 * days) and surfaces the resolved plan + error states inline.
 *
 * The actual fold sweeps appear in the existing sweep dashboard as
 * the orchestrator creates them — this component just kicks the
 * orchestrator off, it doesn't try to re-implement the sweep
 * progress UI.
 */
export function WalkForwardSection({ strategy }: { strategy: StrategyTemplate }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const today = useMemo(() => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }, []);
  const [anchorDate, setAnchorDate] = useState(today);
  const [trainDays, setTrainDays] = useState(60);
  const [testDays, setTestDays] = useState(20);
  const [nFolds, setNFolds] = useState(2);

  const wfMut = useMutation({
    mutationFn: () =>
      triggerWalkForward(strategy.id, {
        anchor_date: anchorDate,
        train_window_days: trainDays,
        test_window_days: testDays,
        n_folds: nFolds,
        rounds_per_discussion: strategy.default_rounds,
        concurrency: strategy.default_concurrency,
        auto_post_mortem: strategy.default_auto_post_mortem,
      }),
    onSuccess: () => {
      // The orchestrator creates fold sweeps as it runs; refresh
      // the sweep list so the user sees them appear.
      queryClient.invalidateQueries({ queryKey: ["sweeps"] });
    },
  });
  const wfResult = wfMut.data as WalkForwardPlanResponse | undefined;
  const errDetail = (
    wfMut.error as { response?: { data?: { detail?: string } } } | undefined
  )?.response?.data?.detail ?? (wfMut.error as Error | undefined)?.message;

  return (
    <div className="text-[10px] space-y-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-purple-300 hover:text-purple-200"
        title={t(
          "strategy.walk_forward_tooltip",
          "用滾動視窗 train/test 切割做 OOS 驗證,結果會出現在下方 sweep 列表",
        )}
      >
        {open
          ? t("strategy.walk_forward_hide", "▼ 收起 OOS 驗證")
          : (
            <span className="inline-flex items-center gap-1">
              <Microscope className="h-3 w-3" />
              {t("strategy.walk_forward_show", "OOS 驗證 (Walk-Forward)")}
            </span>
          )}
      </button>
      {open ? (
        <div
          className="grid grid-cols-2 sm:grid-cols-4 gap-1 bg-secondary/30
                     border border-border rounded p-2"
        >
          <label className="flex flex-col">
            <span className="text-muted-foreground">
              {t("strategy.wf_anchor_date", "Anchor (test 結束日)")}
            </span>
            <input
              type="date"
              value={anchorDate}
              onChange={(e) => setAnchorDate(e.target.value)}
              className="bg-background border border-border rounded px-1 py-0.5"
            />
          </label>
          <label className="flex flex-col">
            <span className="text-muted-foreground">
              {t("strategy.wf_n_folds", "Fold 數 (1-6)")}
            </span>
            <input
              type="number"
              min={1}
              max={6}
              value={nFolds}
              onChange={(e) => setNFolds(Number(e.target.value))}
              className="bg-background border border-border rounded px-1 py-0.5"
            />
          </label>
          <label className="flex flex-col">
            <span className="text-muted-foreground">
              {t("strategy.wf_train_days", "Train 天數")}
            </span>
            <input
              type="number"
              min={1}
              max={120}
              value={trainDays}
              onChange={(e) => setTrainDays(Number(e.target.value))}
              className="bg-background border border-border rounded px-1 py-0.5"
            />
          </label>
          <label className="flex flex-col">
            <span className="text-muted-foreground">
              {t("strategy.wf_test_days", "Test 天數")}
            </span>
            <input
              type="number"
              min={1}
              max={120}
              value={testDays}
              onChange={(e) => setTestDays(Number(e.target.value))}
              className="bg-background border border-border rounded px-1 py-0.5"
            />
          </label>
          <div className="col-span-2 sm:col-span-4 flex items-center gap-2">
            <button
              type="button"
              onClick={() => wfMut.mutate()}
              disabled={wfMut.isPending}
              className="bg-purple-700/40 border border-purple-700/70
                         hover:bg-purple-700/60 text-purple-100 rounded
                         px-2 py-0.5 disabled:opacity-50"
            >
              {wfMut.isPending
                ? t("strategy.wf_submitting", "啟動中…")
                : (
                  <span className="inline-flex items-center gap-1">
                    <Rocket className="h-3 w-3" />
                    {t("strategy.wf_submit", "啟動 Walk-Forward")}
                  </span>
                )}
            </button>
            {wfResult ? (
              <span className="text-success">
                {t(
                  "strategy.wf_success",
                  "✔ 已排定 {{n}} 個 fold,最早 train={{first}} → 最後 test 結束={{last}}",
                  {
                    n: wfResult.folds.length,
                    first:
                      wfResult.folds[wfResult.folds.length - 1]?.train_anchor ??
                      "?",
                    last:
                      wfResult.folds[0]?.test_dates.slice(-1)[0] ?? "?",
                  },
                )}
              </span>
            ) : null}
            {errDetail ? (
              <span className="text-danger">⚠ {errDetail}</span>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
