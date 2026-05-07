import { useEffect, useState } from "react";

import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import {
  useFinmindChain,
  type FinmindChainState,
} from "@/hooks/useFinmindChain";
import { errorDetail } from "@/lib/api";

/**
 * AdminPage card surfacing FinMind 全量回填 chain control + live
 * progress. The 12-dataset 10-year backfill used to be driven only
 * by `/opt/finceptweb/finmind_chain.sh` (host shell + nohup). This
 * card lifts both the monitoring view (current dataset, chunks done,
 * quota gauge) and the start/soft-stop controls into the UI so the
 * operator doesn't need shell access.
 *
 * Backed by `/api/admin/finmind/chain*` (see api/admin/finmind_chain.py).
 */

function formatPct(num: number, denom: number): string {
  if (denom <= 0) return "0%";
  return `${Math.round((num / denom) * 100)}%`;
}

function StatusBanner({ state }: { state: FinmindChainState | undefined }) {
  if (!state) {
    return (
      <div className="rounded border border-border bg-muted/30 p-3 text-sm text-muted-foreground">
        載入中…
      </div>
    );
  }
  const label =
    state.status === "running"
      ? "正在抓取"
      : state.status === "stopping"
        ? "正在停止…"
        : "閒置中";
  const cls =
    state.status === "running"
      ? "border-green-400 bg-green-50 dark:bg-green-950"
      : state.status === "stopping"
        ? "border-amber-400 bg-amber-50 dark:bg-amber-950"
        : "border-border bg-muted/30";
  return (
    <div className={`rounded border p-3 text-sm ${cls}`}>
      <div className="font-semibold">{label}</div>
      {state.current_dataset && (
        <div className="mt-1 font-mono text-xs">
          {state.current_dataset}
          {state.current_symbol ? ` · ${state.current_symbol}` : ""}
        </div>
      )}
      {state.last_chunk_at && (
        <div className="mt-0.5 text-xs text-muted-foreground">
          最後一批: {new Date(state.last_chunk_at).toLocaleString()}
        </div>
      )}
    </div>
  );
}

function ProgressBar({ done, total }: { done: number; total: number }) {
  const pct = total > 0 ? Math.min(100, (done / total) * 100) : 0;
  return (
    <div className="h-2 w-full overflow-hidden rounded bg-muted">
      <div
        className="h-full bg-primary transition-all"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function QuotaGauge({
  used,
  limit,
}: {
  used: number | null;
  limit: number;
}) {
  const u = used ?? 0;
  const pct = limit > 0 ? Math.min(100, (u / limit) * 100) : 0;
  const color =
    pct >= 90
      ? "bg-destructive"
      : pct >= 70
        ? "bg-amber-500"
        : "bg-primary";
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs">
        <span className="text-muted-foreground">FinMind 每小時 quota</span>
        <span className="font-mono">
          {u.toLocaleString()} / {limit.toLocaleString()} ({formatPct(u, limit)})
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded bg-muted">
        <div
          className={`h-full transition-all ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function FinmindBackfillCard() {
  const { open, toggle } = useCollapsible("admin-finmind-backfill", true);
  const { stateQuery, start, stop, resetStuck } = useFinmindChain(open);
  const s = stateQuery.data;

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [days, setDays] = useState<number>(3650);

  // Sync the checklist defaults with whatever the backend reports
  // as DEFAULT_DATASETS the first time the state loads. This keeps
  // the frontend honest if we ever add / remove a dataset from the
  // backend list — no stale hard-coded list to maintain here.
  useEffect(() => {
    if (s?.default_datasets && selected.size === 0) {
      setSelected(new Set(s.default_datasets));
    }
  }, [s?.default_datasets, selected.size]);

  const isRunning = s?.status === "running";
  const isStopping = s?.status === "stopping";
  const blockedByHost = !!s?.host_chain_likely_active;
  const startDisabled =
    isRunning || isStopping || blockedByHost || selected.size === 0 || start.isPending;
  const stopDisabled = !isRunning || stop.isPending;

  function toggleDataset(code: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  }

  function handleStart() {
    start.mutate({
      datasets: Array.from(selected),
      days,
      reset_stuck_first: true,
    });
  }

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <CollapsibleHeader
        title="FinMind 全量回填監控"
        subtitle={
          s
            ? `${s.status === "running" ? "正在抓取" : s.status === "stopping" ? "正在停止" : "閒置中"} · ${s.chunks_done}/${s.chunks_total} chunks`
            : "click to expand"
        }
        open={open}
        toggle={toggle}
      />

      {open && (
        <div className="mt-4 space-y-4">
          {blockedByHost && (
            <div className="rounded border border-amber-400 bg-amber-50 p-3 text-xs dark:bg-amber-950">
              <div className="font-semibold">
                偵測到外部 backfill 正在進行中
              </div>
              <div className="mt-1 text-muted-foreground">
                10 分鐘內有其他來源(host 上的 finmind_chain.sh /
                finmind_chain_all.sh、或 backend 內 APScheduler 的 daily
                refresh)正在 claim chunk,但本次 UI 沒持有 chain lock。等
                目前在跑的 chunk 結束(通常 1-2 分鐘),或先按下方「清理
                卡住的 chunk」把 stale 的 running 重設成 pending,再重試。
                兩條 chain 同時跑會雙重消耗 FinMind 6000/hr quota 觸發 402
                Payment Required。
              </div>
            </div>
          )}

          <StatusBanner state={s} />

          {s && s.chunks_total > 0 && (
            <div>
              <div className="mb-1 flex justify-between text-xs">
                <span className="text-muted-foreground">
                  目前 dataset 進度
                </span>
                <span className="font-mono">
                  {s.chunks_done}/{s.chunks_total} ({formatPct(s.chunks_done, s.chunks_total)})
                  {s.chunks_failed > 0 && (
                    <span className="ml-2 text-destructive">
                      失敗 {s.chunks_failed}
                    </span>
                  )}
                </span>
              </div>
              <ProgressBar done={s.chunks_done} total={s.chunks_total} />
            </div>
          )}

          {s && (
            <QuotaGauge used={s.quota_used} limit={s.quota_limit} />
          )}

          {/* Dataset checklist ───────────────────────────────── */}
          {s?.default_datasets && (
            <div className="rounded border border-border p-3">
              <div className="mb-2 flex items-center justify-between text-sm">
                <span className="font-semibold">要抓取的 dataset</span>
                <span className="text-xs text-muted-foreground">
                  {selected.size} / {s.default_datasets.length} 已勾選
                </span>
              </div>
              <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                {s.default_datasets.map((code) => (
                  <label
                    key={code}
                    className="flex items-center gap-2 text-xs"
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(code)}
                      onChange={() => toggleDataset(code)}
                      disabled={isRunning || isStopping}
                    />
                    <span className="font-mono">{code}</span>
                  </label>
                ))}
              </div>
              <div className="mt-3 flex items-center gap-2 text-xs">
                <label className="flex items-center gap-2">
                  回填天數
                  <input
                    type="number"
                    min={1}
                    max={3650 * 2}
                    value={days}
                    onChange={(e) =>
                      setDays(Math.max(1, Number(e.target.value) || 1))
                    }
                    disabled={isRunning || isStopping}
                    className="w-24 rounded border border-border bg-background px-2 py-0.5"
                  />
                </label>
                <span className="text-muted-foreground">
                  3650 = 10 年 (預設)
                </span>
              </div>
            </div>
          )}

          {/* Controls ─────────────────────────────────────────── */}
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={handleStart}
              disabled={startDisabled}
              className="rounded bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {start.isPending ? "啟動中…" : "開始回填"}
            </button>
            <button
              type="button"
              onClick={() => stop.mutate()}
              disabled={stopDisabled}
              className="rounded border border-destructive bg-destructive/10 px-3 py-1.5 text-sm text-destructive hover:bg-destructive/20 disabled:opacity-50"
            >
              {stop.isPending ? "停止中…" : "停止 (跑完目前 chunk)"}
            </button>
            <button
              type="button"
              onClick={() => resetStuck.mutate()}
              disabled={resetStuck.isPending}
              className="rounded border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
            >
              {resetStuck.isPending ? "清理中…" : "清理卡住的 chunk"}
            </button>
            {resetStuck.data && (
              <span className="text-xs text-muted-foreground">
                已清理 {resetStuck.data.reset} 條 stuck running chunk
              </span>
            )}
          </div>

          {start.isError && (
            <div className="rounded border border-destructive bg-destructive/10 p-2 text-xs">
              啟動失敗: {errorDetail(start.error)}
            </div>
          )}
          {stop.isError && (
            <div className="rounded border border-destructive bg-destructive/10 p-2 text-xs">
              停止失敗: {errorDetail(stop.error)}
            </div>
          )}
          {resetStuck.isError && (
            <div className="rounded border border-destructive bg-destructive/10 p-2 text-xs">
              清理失敗: {errorDetail(resetStuck.error)}
            </div>
          )}

          {/* Recent errors ────────────────────────────────────── */}
          {s?.recent_errors && s.recent_errors.length > 0 && (
            <div>
              <h3 className="mb-1 text-sm font-semibold">最近錯誤</h3>
              <ul className="space-y-1 text-xs">
                {s.recent_errors.map((e, i) => (
                  <li
                    key={i}
                    className="rounded border border-border bg-muted/30 p-2 break-all"
                  >
                    {e}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Queue ────────────────────────────────────────────── */}
          {s && s.queue.length > 0 && (
            <div className="text-xs text-muted-foreground">
              佇列中還有 {s.queue.length} 個 dataset:{" "}
              <span className="font-mono">{s.queue.join(", ")}</span>
            </div>
          )}

          {stateQuery.isError && (
            <div className="rounded border border-destructive bg-destructive/10 p-2 text-xs">
              無法讀取 chain 狀態: {errorDetail(stateQuery.error)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
