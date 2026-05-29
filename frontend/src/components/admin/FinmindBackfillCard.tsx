import { useState } from "react";

import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import {
  useFinmindChain,
  type FinmindChainState,
  type PerDatasetProgress,
} from "@/hooks/useFinmindChain";
import { errorDetail } from "@/lib/api";
import { formatTaipei } from "@/lib/timeFormat";

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
          最後一批: {formatTaipei(state.last_chunk_at)}
        </div>
      )}
    </div>
  );
}

function PerDatasetTable({
  rows,
  currentDataset,
}: {
  rows: PerDatasetProgress[];
  currentDataset: string | null;
}) {
  if (rows.length === 0) return null;
  return (
    <div>
      <div className="mb-1 flex justify-between text-sm">
        <span className="font-semibold">每個 dataset 進度</span>
        <span className="text-xs text-muted-foreground">
          row 數為估計值 (pg_class.reltuples)
        </span>
      </div>
      <div className="overflow-x-auto rounded border border-border">
        <table className="w-full text-xs">
          <thead className="bg-muted/40 text-muted-foreground">
            <tr>
              <th className="px-2 py-1 text-left font-medium">Dataset</th>
              <th className="px-2 py-1 text-right font-medium">Chunks</th>
              <th className="px-2 py-1 text-right font-medium">%</th>
              <th className="px-2 py-1 text-right font-medium">失敗</th>
              <th className="px-2 py-1 text-right font-medium">資料表 row 數</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const pct = formatPct(r.chunks_done, r.chunks_total);
              const isCurrent = r.dataset === currentDataset;
              const fullyDone =
                r.chunks_total > 0 && r.chunks_done >= r.chunks_total;
              return (
                <tr
                  key={r.dataset}
                  className={`border-t border-border ${
                    isCurrent ? "bg-primary/10" : ""
                  }`}
                >
                  <td className="px-2 py-1 font-mono">
                    {r.dataset}
                    {r.local_table && (
                      <span className="ml-1 text-muted-foreground">
                        → {r.local_table}
                      </span>
                    )}
                  </td>
                  <td className="px-2 py-1 text-right font-mono">
                    {r.chunks_done.toLocaleString()}/
                    {r.chunks_total.toLocaleString()}
                  </td>
                  <td
                    className={`px-2 py-1 text-right font-mono ${
                      fullyDone ? "text-green-600 dark:text-green-400" : ""
                    }`}
                  >
                    {pct}
                  </td>
                  <td
                    className={`px-2 py-1 text-right font-mono ${
                      r.chunks_failed > 0 ? "text-destructive" : "text-muted-foreground"
                    }`}
                  >
                    {r.chunks_failed.toLocaleString()}
                  </td>
                  <td className="px-2 py-1 text-right font-mono">
                    {r.row_count == null
                      ? "—"
                      : `≈ ${r.row_count.toLocaleString()}`}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
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
  globalLimit,
}: {
  used: number | null;
  limit: number;
  globalLimit: number;
}) {
  const u = used ?? 0;
  const pct = limit > 0 ? Math.min(100, (u / limit) * 100) : 0;
  const color =
    pct >= 90
      ? "bg-destructive"
      : pct >= 70
        ? "bg-amber-500"
        : "bg-primary";
  // Reservation = 全域 cap 跟 chain 預算之間的差額,留給討論 / screener
  // / news 等非 chain 路徑用。當兩者相等時不顯示(沒分離)。
  const reserved = Math.max(0, globalLimit - limit);
  const reservedPct =
    globalLimit > 0 ? (reserved / globalLimit) * 100 : 0;
  const chainPct =
    globalLimit > 0 ? Math.min(100, (limit / globalLimit) * 100) : 0;
  const usedOfGlobalPct =
    globalLimit > 0 ? Math.min(100, (u / globalLimit) * 100) : 0;
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs">
        <span className="text-muted-foreground">
          FinMind 每小時 quota(chain 預算)
        </span>
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
      {reserved > 0 && (
        <div className="mt-2">
          <div className="mb-1 flex justify-between text-xs">
            <span className="text-muted-foreground">
              全域 cap(含預留 {reserved.toLocaleString()}/hr 給討論等)
            </span>
            <span className="font-mono">
              {u.toLocaleString()} / {globalLimit.toLocaleString()} ({formatPct(u, globalLimit)})
            </span>
          </div>
          <div className="relative h-2 w-full overflow-hidden rounded bg-muted">
            {/* chain budget fill (matches the same color as the gauge above) */}
            <div
              className={`absolute inset-y-0 left-0 transition-all ${color}`}
              style={{ width: `${usedOfGlobalPct}%` }}
            />
            {/* visual marker at the chain-budget boundary */}
            <div
              className="absolute inset-y-0 w-px bg-foreground/40"
              style={{ left: `${chainPct}%` }}
              title={`chain 預算邊界 (${limit.toLocaleString()}/hr)`}
            />
            {/* reservation band, dim hatch */}
            <div
              className="absolute inset-y-0 right-0 bg-foreground/10"
              style={{ width: `${reservedPct}%` }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

export default function FinmindBackfillCard() {
  const { open, toggle } = useCollapsible("admin-finmind-backfill", true);
  const { stateQuery, start, stop, resetStuck } = useFinmindChain(open);
  const s = stateQuery.data;

  // `userSelection === null` means "user hasn't customised yet, fall
  // back to the backend's DEFAULT_DATASETS list". Computing `selected`
  // during render (rather than syncing via useEffect → setState)
  // avoids the cascading-render anti-pattern flagged by
  // react-hooks/set-state-in-effect, and keeps the checklist honest
  // if the backend ever changes the default list.
  const [userSelection, setUserSelection] = useState<Set<string> | null>(
    null,
  );
  const selected =
    userSelection ?? new Set<string>(s?.default_datasets ?? []);
  const [days, setDays] = useState<number>(365);

  const isRunning = s?.status === "running";
  const isStopping = s?.status === "stopping";
  // External activity (host script or in-container APScheduler daily
  // refresh) is informational — we still let the user click Start, the
  // chain's pre-flight quota gate handles saturation gracefully. Only
  // a real duplicate UI start is hard-blocked (via the redis lock,
  // which surfaces as a 409 on the start request).
  const externalActivity = !!s?.external_activity_detected;
  const startDisabled =
    isRunning || isStopping || selected.size === 0 || start.isPending;
  const stopDisabled = !isRunning || stop.isPending;

  function toggleDataset(code: string) {
    setUserSelection((prev) => {
      const base = prev ?? new Set<string>(s?.default_datasets ?? []);
      const next = new Set(base);
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
            ? `${s.status === "running" ? "正在抓取" : s.status === "stopping" ? "正在停止" : "閒置中"} · ${s.total_chunks_done.toLocaleString()}/${s.total_chunks_total.toLocaleString()} chunks (${formatPct(s.total_chunks_done, s.total_chunks_total)})`
            : "click to expand"
        }
        open={open}
        toggle={toggle}
      />

      {open && (
        <div className="mt-4 space-y-4">
          {externalActivity && (
            <div className="rounded border border-amber-400 bg-amber-50 p-3 text-xs dark:bg-amber-950">
              <div className="font-semibold">
                目前有其他 backfill 任務在使用 FinMind quota
              </div>
              <div className="mt-1 text-muted-foreground">
                10 分鐘內偵測到外部來源(host 腳本或 backend 內 APScheduler
                的 daily refresh)正在 claim chunk。本次 chain 仍可啟動 ──
                pre-flight quota gate 會在 chain 預算飽和時自動 sleep,
                不會硬撞。但兩邊一起跑會共用 quota,進度條會比預期慢。若 UI
                曾意外中斷讓 lock 殘留,下一次 start 會自動 reset stuck →
                重啟。
              </div>
            </div>
          )}

          <StatusBanner state={s} />

          {/* Overall progress — sum across every dataset in this
              chain run, not just the currently-running one. Always
              renders when selected_datasets is non-empty (i.e. a
              chain has been started), so the user can review final
              totals after the run ends too. */}
          {s && s.selected_datasets.length > 0 && (
            <div>
              <div className="mb-1 flex justify-between text-sm">
                <span className="font-semibold">
                  整體進度
                </span>
                <span className="font-mono">
                  {s.total_chunks_done.toLocaleString()} /{" "}
                  {s.total_chunks_total.toLocaleString()} (
                  {formatPct(s.total_chunks_done, s.total_chunks_total)})
                </span>
              </div>
              <ProgressBar
                done={s.total_chunks_done}
                total={s.total_chunks_total}
              />
              <div className="mt-1 text-xs text-muted-foreground">
                {s.selected_datasets.length} 個 dataset × {s.universe_size.toLocaleString()} symbols
                {s.queue.length > 0 && (
                  <> · 佇列剩 {s.queue.length} 個 dataset</>
                )}
              </div>
            </div>
          )}

          {s && s.per_dataset_progress && s.per_dataset_progress.length > 0 && (
            <PerDatasetTable
              rows={s.per_dataset_progress}
              currentDataset={s.current_dataset}
            />
          )}

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
            <QuotaGauge
              used={s.quota_used}
              limit={s.quota_limit}
              globalLimit={s.quota_limit_global}
            />
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
                  365 = 1 年 (預設)
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
