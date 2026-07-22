import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { AlertTriangle, CalendarDays, ShieldAlert, Users } from "lucide-react";
import { getPersonaIdentity } from "@/components/discussion/personaIdentity";
import { BAND_LABELS, classifySymbolBand } from "@/components/discussion/format/symbols";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { OHLCVBar } from "@/types/market";
import { dedupeBySymbol } from "./dailyCandidates";

// lightweight-charts only loads once a visitor actually opens a chart —
// the public page itself stays lean.
const CandlestickChart = lazy(() => import("@/components/charts/CandlestickChart"));

type QualitySignals = {
  consensus_contradiction?: boolean;
  hallucination_warnings?: Array<unknown>;
  confidence_stats?: { over_confident?: boolean };
  _skipped?: string;
};
type Conclusion = {
  recommended_symbols?: string[];
  symbol_names?: Record<string, string>;
  recommendations?: Array<{ symbol: string; confidence: number; calibrated_confidence?: number }>;
  reasoning?: string;
  risks?: string[];
  time_horizon?: string;
  consensus_score?: number;
  quality_signals?: QualitySignals;
};
type Turn = { round: number; turn_index: number; persona_id: string; persona_name: string; stance: string; content: string };
type Result = {
  market: string;
  topic: string;
  created_at: string;
  captured_session: { session_date?: string | null; hint_zh?: string } | null;
  conclusion: Conclusion;
  turns: Turn[];
  strategy?: string;
  sequence?: number;
  candidates?: Array<{ symbol?: string; name?: string; strategy_score?: number; signal_type?: string }>;
  candidate_pool?: Array<{ symbol?: string; name?: string; strategy_score?: number; signal_type?: string }>;
  verdict?: "big_win" | "win" | "big_loss" | "loss" | "abstain" | "unverifiable" | null;
  verdict_reason?: string | null;
  verified_at?: string | null;
  verify_after_date?: string | null;
  day1_open_prices?: Record<string, number> | null;
  day5_close_prices?: Record<string, number> | null;
  daily_close_prices?: Record<string, (number | null)[]> | null;
};
type DailyDay = { date: string; strategies: Record<string, Result[]> };
type Payload = { state: "disabled" | "empty" | "ready"; result: Result | null; strategies?: Record<string, Result[]>; days?: DailyDay[]; disclaimer: string };
type ScoreboardEntry = {
  strategy: string;
  samples: number;
  decided?: number;
  pending?: number;
  abstains?: number;
  benchmark_samples?: number;
  avg_excess_vs_taiex_pct?: number | null;
  wins: number;
  losses: number;
  big_wins: number;
  big_losses: number;
  unverifiable: number;
  win_rate?: number | null;
  avg_return_pct?: number | null;
  pool_samples: number;
  avg_alpha_pct?: number | null;
};

const strategyNames: Record<string, string> = { general: "綜合選股", chip_quality: "籌碼品質", price_signal: "量價訊號" };
// Days published before the 5→3 strategy merge still carry these keys;
// their tabs only render while such a day is in the 5-day window.
const legacyStrategyNames: Record<string, string> = { chip_momentum: "籌碼動能", quality_growth: "品質成長", breakout: "突破追價", oversold_reversal: "超跌反轉" };
const signalNames: Record<string, string> = { breakout: "突破", oversold: "超跌" };

const horizon: Record<string, string> = {
  short_term: "短期", medium_term: "中期", long_term: "長期",
};
const stance: Record<string, string> = { agree: "贊同", dissent: "異議", supplement: "補充" };
const stanceTone: Record<string, string> = {
  agree: "border-success/30 bg-success/10 text-success",
  dissent: "border-danger/30 bg-danger/10 text-danger",
  supplement: "border-info/30 bg-info/10 text-info",
};

function qualityWarnings(q?: QualitySignals): string[] {
  if (!q) return [];
  const items: string[] = [];
  if (q.consensus_contradiction) items.push("發言立場與結論可能互相矛盾");
  if (q.confidence_stats?.over_confident) items.push("推薦信心可能過度集中或偏高");
  if (q.hallucination_warnings?.length) items.push(`偵測到 ${q.hallucination_warnings.length} 項資料引用警示`);
  if (q._skipped) items.push("品質檢查未完整執行");
  return items;
}

export default function DailyPage() {
  const [payload, setPayload] = useState<Payload | null>(null);
  const [scoreboard, setScoreboard] = useState<ScoreboardEntry[]>([]);
  const [error, setError] = useState("");
  const [activeStrategy, setActiveStrategy] = useState("general");
  const [activeDate, setActiveDate] = useState("");
  const [chartSymbol, setChartSymbol] = useState<SelectedSymbol>(null);

  const openChart = useCallback((symbol: string, name?: string) => {
    setChartSymbol({ symbol, name });
  }, []);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/public/daily", { credentials: "omit", headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const next = await res.json() as Payload;
      setPayload(next);
      setActiveDate((current) => current && next.days?.some((day) => day.date === current) ? current : (next.days?.[0]?.date ?? ""));
      setError("");
    } catch {
      setError("目前無法取得每日圓桌結果，請稍後再試。");
    }
    // The scoreboard is a bonus panel — its failure must not blank the page.
    try {
      const res = await fetch("/api/public/daily/scoreboard", { credentials: "omit", headers: { Accept: "application/json" } });
      if (res.ok) {
        const body = await res.json() as { state: string; entries?: ScoreboardEntry[] };
        setScoreboard(body.state === "ready" ? body.entries ?? [] : []);
      }
    } catch {
      /* keep whatever we had */
    }
  }, []);

  useEffect(() => {
    document.title = "每日投資圓桌｜Fincept";
    const upsert = (name: string, content: string) => {
      let el = document.head.querySelector<HTMLMetaElement>(`meta[name="${name}"]`);
      if (!el) { el = document.createElement("meta"); el.name = name; document.head.appendChild(el); }
      el.content = content;
    };
    upsert("description", "查看最新一次每日自動投資圓桌的公開結論與五輪完整發言。");
    upsert("robots", "noindex, nofollow");
    const initialLoad = window.setTimeout(load, 0);
    const timer = window.setInterval(load, 5 * 60 * 1000);
    const focus = () => void load();
    window.addEventListener("focus", focus);
    return () => { window.clearTimeout(initialLoad); window.clearInterval(timer); window.removeEventListener("focus", focus); };
  }, [load]);

  const result = payload?.result;
  const days = payload?.days ?? [];
  const selectedDay = days.find((day) => day.date === activeDate) ?? days[0];
  const strategies = selectedDay?.strategies ?? payload?.strategies ?? {};
  const strategyTabs: Array<[string, string]> = [
    ...Object.entries(strategyNames),
    ...Object.entries(legacyStrategyNames).filter(([key]) => (strategies[key] ?? []).length > 0),
  ];
  const currentStrategy = strategyTabs.some(([key]) => key === activeStrategy) ? activeStrategy : "general";
  const activeResults = strategies[currentStrategy] ?? [];

  return (
    <main className="min-h-screen bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-5xl px-4 py-10 sm:px-6 sm:py-16">
        {!payload && !error && <StateCard>正在取得最新結果…</StateCard>}
        {error && <StateCard icon={<AlertTriangle className="h-6 w-6 text-amber-400" />} action={load}>{error}</StateCard>}
        {payload?.state === "disabled" && <StateCard icon={<ShieldAlert className="h-6 w-6" />}>每日公開結果尚未設定。</StateCard>}
        {payload?.state === "empty" && <StateCard icon={<CalendarDays className="h-6 w-6" />}>目前尚無可公開的成功討論結果。</StateCard>}

        {payload?.state === "ready" && result && (
          <div className="space-y-8">
            <Scoreboard entries={scoreboard} />
            {days.length > 0 && <nav aria-label="最近七個工作日" className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
              {days.map((day, index) => {
                const selected = day.date === (selectedDay?.date ?? activeDate);
                const date = new Date(`${day.date}T00:00:00+08:00`);
                return <button key={day.date} onClick={() => setActiveDate(day.date)} className={`rounded-xl border px-3 py-3 text-left transition ${selected ? "border-success bg-success/10 text-white" : "border-slate-800 bg-slate-900 text-slate-400 hover:border-slate-700"}`}>
                  <span className="block text-xs">{index === 0 ? "最新工作日" : date.toLocaleDateString("zh-TW", { weekday: "short" })}</span>
                  <span className="mt-1 block font-semibold">{date.toLocaleDateString("zh-TW", { month: "numeric", day: "numeric" })}</span>
                </button>;
              })}
            </nav>}
            <nav role="tablist" aria-label="每日選股策略" className="flex gap-2 overflow-x-auto border-b border-slate-800 pb-3">
              {strategyTabs.map(([key, name]) => <button key={key} role="tab" aria-selected={currentStrategy === key} onClick={() => setActiveStrategy(key)} className={`shrink-0 rounded-xl px-4 py-2.5 text-sm font-semibold transition ${currentStrategy === key ? "bg-success text-slate-950" : "bg-slate-900 text-slate-400 hover:text-white"}`}>
                {name}<span className={`ml-2 rounded-full px-2 py-0.5 text-xs ${currentStrategy === key ? "bg-slate-950/15" : "bg-slate-800"}`}>{(strategies[key] ?? []).length}</span>
              </button>)}
            </nav>

            <section role="tabpanel" aria-label={strategyNames[currentStrategy] ?? legacyStrategyNames[currentStrategy]}>
              {activeResults.length === 0 ? <StateCard icon={<CalendarDays className="h-6 w-6" />}>今日無符合安全條件的候選股。</StateCard> : <div className="space-y-6"><CandidatePool results={activeResults} onSelect={openChart} />{activeResults.map((run) => <StrategyRun key={`${currentStrategy}-${run.sequence}`} run={run} onSelect={openChart} />)}</div>}
            </section>

            <footer className="border-t border-slate-800 pt-6 text-xs leading-6 text-slate-500">{payload.disclaimer}</footer>
          </div>
        )}
      </div>
      <SymbolChartDialog selected={chartSymbol} onClose={() => setChartSymbol(null)} />
    </main>
  );
}

type SelectedSymbol = { symbol: string; name?: string } | null;

function SymbolChartDialog({ selected, onClose }: { selected: SelectedSymbol; onClose: () => void }) {
  return (
    <Dialog open={!!selected} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-3xl border-slate-800 bg-slate-950 text-slate-100">
        <DialogHeader>
          <DialogTitle className="text-slate-100">
            {selected ? `${selected.symbol}${selected.name ? ` ${selected.name}` : ""} 日 K 線（近六個月）` : ""}
          </DialogTitle>
        </DialogHeader>
        {/* Keyed by symbol so switching stocks remounts with fresh state
            instead of resetting it synchronously inside an effect. */}
        {selected && <SymbolChartBody key={selected.symbol} symbol={selected.symbol} />}
      </DialogContent>
    </Dialog>
  );
}

function SymbolChartBody({ symbol }: { symbol: string }) {
  const [bars, setBars] = useState<OHLCVBar[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/public/daily/history/${symbol}`, { credentials: "omit", headers: { Accept: "application/json" } });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json() as OHLCVBar[];
        if (!cancelled) setBars(data.filter((b) => b.open != null && b.high != null && b.low != null && b.close != null));
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();
    return () => { cancelled = true; };
  }, [symbol]);

  if (failed) return <div className="py-16 text-center text-sm text-slate-400">目前無法取得 K 線資料，請稍後再試。</div>;
  if (!bars) return <div className="py-16 text-center text-sm text-slate-500">正在載入 K 線…</div>;
  if (!bars.length) return <div className="py-16 text-center text-sm text-slate-400">此標的暫無歷史價格資料。</div>;
  return (
    <Suspense fallback={<div className="py-16 text-center text-sm text-slate-500">正在載入圖表…</div>}>
      {/* Deliberately no market/symbol props: they switch on the
          authed drawings/alerts wiring, which would 401 for the
          anonymous visitors of this page. */}
      <CandlestickChart bars={bars} height={420} />
    </Suspense>
  );
}

function StateCard({ children, icon, action }: { children: React.ReactNode; icon?: React.ReactNode; action?: () => void }) {
  return <div className="rounded-2xl border border-slate-800 bg-slate-900 p-8 text-center text-slate-300">{icon && <div className="mb-3 flex justify-center">{icon}</div>}<p>{children}</p>{action && <button onClick={action} className="mt-5 rounded-lg bg-success px-4 py-2 text-sm font-semibold text-slate-950">重試</button>}</div>;
}
function Info({ label, value }: { label: string; value: string }) {
  return <div className="rounded-xl bg-slate-950/50 p-4"><div className="text-xs text-slate-500">{label}</div><div className="mt-1 font-semibold">{value}</div></div>;
}

function Scoreboard({ entries }: { entries: ScoreboardEntry[] }) {
  if (!entries.length) return null;
  const pct = (value?: number | null, signed = false) =>
    typeof value === "number" ? `${signed && value >= 0 ? "+" : ""}${value.toFixed(1)}%` : "—";
  return <details className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
    <summary className="cursor-pointer text-sm font-semibold text-slate-300">策略計分板（歷史勝率與五日報酬）</summary>
    <div className="mt-4 overflow-x-auto">
      <table className="w-full min-w-[560px] text-sm">
        <thead>
          <tr className="text-left text-xs text-slate-500">
            <th className="py-1.5 pr-4 font-medium">策略</th>
            <th className="py-1.5 pr-4 font-medium">勝率</th>
            <th className="py-1.5 pr-4 font-medium">平均五日報酬</th>
            <th className="py-1.5 pr-4 font-medium">大勝／大敗</th>
            <th className="py-1.5 pr-4 font-medium">樣本</th>
            <th className="py-1.5 pr-4 font-medium">AI−池 α</th>
            <th className="py-1.5 font-medium">AI−大盤</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => {
            const decided = entry.decided ?? entry.wins + entry.losses;
            // A win rate over a handful of decided rounds is noise, and
            // showing a bare "100%" next to it is how the page ended up
            // overstating the panel. Under 10 decided rounds the number
            // is explicitly marked as not yet meaningful.
            const thin = decided < 10;
            return <tr key={entry.strategy} className="border-t border-slate-800/60 text-slate-200">
              <td className="py-2 pr-4 font-semibold">
                {strategyNames[entry.strategy] ?? legacyStrategyNames[entry.strategy] ?? entry.strategy}
                {thin && <span className="ml-2 rounded-full border border-slate-700 px-1.5 py-0.5 text-micro text-slate-500">樣本不足</span>}
              </td>
              <td className="py-2 pr-4">
                <span className={thin ? "text-slate-400" : undefined}>
                  {typeof entry.win_rate === "number" ? `${Math.round(entry.win_rate * 100)}%` : "—"}
                </span>
                {decided > 0 && <span className="ml-1 text-xs text-slate-500">({entry.wins}勝{entry.losses}敗／決勝 {decided} 場)</span>}
              </td>
              <td className={`py-2 pr-4 ${typeof entry.avg_return_pct === "number" ? (entry.avg_return_pct >= 0 ? "text-up" : "text-down") : ""}`}>
                {pct(entry.avg_return_pct, true)}
              </td>
              <td className="py-2 pr-4 text-xs">{entry.big_wins}／{entry.big_losses}</td>
              <td className="py-2 pr-4">
                {entry.samples}
                <span className="ml-1 text-xs text-slate-500">
                  ({[
                    (entry.pending ?? 0) > 0 ? `待判 ${entry.pending}` : null,
                    (entry.abstains ?? 0) > 0 ? `棄權 ${entry.abstains}` : null,
                    entry.unverifiable > 0 ? `無法驗證 ${entry.unverifiable}` : null,
                  ].filter(Boolean).join("／") || `決勝 ${decided}`})
                </span>
              </td>
              <td className={`py-2 pr-4 ${typeof entry.avg_alpha_pct === "number" ? (entry.avg_alpha_pct >= 0 ? "text-up" : "text-down") : "text-slate-500"}`}>
                {entry.pool_samples > 0 ? pct(entry.avg_alpha_pct, true) : "累積中"}
              </td>
              <td className={`py-2 ${typeof entry.avg_excess_vs_taiex_pct === "number" ? (entry.avg_excess_vs_taiex_pct >= 0 ? "text-up" : "text-down") : "text-slate-500"}`}>
                {(entry.benchmark_samples ?? 0) > 0 ? pct(entry.avg_excess_vs_taiex_pct, true) : "累積中"}
              </td>
            </tr>;
          })}
        </tbody>
      </table>
    </div>
    <p className="mt-3 text-xs leading-5 text-slate-500">勝率＝已分出勝負的場次中，第 5 個交易日收盤仍達門檻的比例（期間漲過又跌回來不算勝）；棄權（候選股皆不符合進場條件）與待判、無法驗證都不列入勝率分母。AI−池 α＝AI 精選的五日報酬減去該場完整候選池的平均報酬；AI−大盤＝減去同期間加權報酬指數。決勝場次少於 10 場時數字僅供參考，不具統計意義。歷史統計不構成投資建議。</p>
  </details>;
}

function CandidatePool({ results, onSelect }: { results: Result[]; onSelect: (symbol: string, name?: string) => void }) {
  const stored = results.find((r) => (r.candidate_pool ?? []).length > 0)?.candidate_pool;
  // Days published before pool snapshots existed fall back to the batches
  // that actually entered discussions (deduped across runs).
  const items = stored ?? dedupeBySymbol(results.flatMap((r) => r.candidates ?? []));
  if (!items.length) return null;
  return <details className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
    <summary className="cursor-pointer text-sm font-semibold text-slate-300">
      完整候選池（{items.length} 支）{!stored && <span className="ml-2 font-normal text-xs text-slate-500">僅顯示已進入討論的批次候選</span>}
    </summary>
    <div className="mt-4 flex flex-wrap gap-2">{items.map((c) => (
      <button key={c.symbol} type="button" title="查看 K 線圖" onClick={() => c.symbol && onSelect(c.symbol, c.name)} className="rounded-full border border-slate-700 bg-slate-950/60 px-3 py-1 text-xs text-slate-300 transition hover:border-slate-500 hover:text-white">
        {c.symbol}{c.name && <span className="ml-1 text-slate-400">{c.name}</span>}
        {c.signal_type && signalNames[c.signal_type] && <span className="ml-1 text-amber-300">{signalNames[c.signal_type]}</span>}
      </button>
    ))}</div>
  </details>;
}

function StrategyRun({ run, onSelect }: { run: Result; onSelect: (symbol: string, name?: string) => void }) {
  const warnings = qualityWarnings(run.conclusion.quality_signals);
  const signalCandidates = (run.candidates ?? []).filter((c) => c.symbol && c.signal_type && signalNames[c.signal_type]);
  return <article className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5 sm:p-7">
    {signalCandidates.length > 0 && <div className="mb-4 flex flex-wrap gap-2">{signalCandidates.map((c) => <button key={c.symbol} type="button" title="查看 K 線圖" onClick={() => onSelect(c.symbol!, c.name)} className="rounded-full border border-slate-700 bg-slate-950/60 px-3 py-1 text-xs text-slate-300 transition hover:border-slate-500 hover:text-white">{c.symbol}{c.name && <span className="ml-1 text-slate-400">{c.name}</span>} · {signalNames[c.signal_type!]}</button>)}</div>}
    <OutcomeSummary run={run} onSelect={onSelect} />
    <div className="mt-4 flex flex-wrap gap-3">{(run.conclusion.recommended_symbols ?? []).map((symbol) => <button key={symbol} type="button" title="查看 K 線圖" onClick={() => onSelect(symbol, run.conclusion.symbol_names?.[symbol])} className="rounded-xl bg-amber-900/40 px-4 py-3 text-left text-amber-100 ring-1 ring-amber-800/60 transition hover:bg-amber-900/60 hover:ring-amber-600"><div className="font-bold">{symbol}</div><div className="text-xs text-amber-200/70">{run.conclusion.symbol_names?.[symbol] ?? ""}</div></button>)}{!(run.conclusion.recommended_symbols?.length) && <p className="text-slate-400">本場沒有推薦標的</p>}</div>
    <p className="mt-5 whitespace-pre-wrap leading-7 text-slate-200">{run.conclusion.reasoning}</p>
    <div className="mt-5 grid gap-3 sm:grid-cols-2"><Info label="時間框架" value={horizon[run.conclusion.time_horizon ?? ""] ?? run.conclusion.time_horizon ?? "—"} /><Info label="共識度" value={typeof run.conclusion.consensus_score === "number" ? `${Math.round(run.conclusion.consensus_score * 100)}%` : "—"} /></div>
    {!!warnings.length && <div className="mt-5 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">{warnings.join("；")}</div>}
    <details className="mt-6"><summary className="flex cursor-pointer items-center gap-2 text-sm font-semibold text-slate-300"><Users className="h-4 w-4 text-success" />展開五輪完整發言</summary><div className="mt-4 space-y-3">{run.turns.map((turn) => { const identity = getPersonaIdentity(turn.persona_id, turn.persona_name); return <div key={`${turn.round}-${turn.turn_index}`} className="rounded-xl border border-l-[3px] border-slate-800 bg-slate-950/60 p-4" style={{ borderLeftColor: identity.accentColor }}><div className="flex items-center justify-between gap-3 text-sm"><b style={{ color: identity.accentColor }}>{turn.persona_name}</b><span className={`rounded-full border px-2 py-0.5 text-xs ${stanceTone[turn.stance] ?? "border-slate-700 bg-slate-800 text-slate-400"}`}>第 {turn.round} 輪 · {stance[turn.stance] ?? turn.stance}</span></div><p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-300">{turn.content || "（本輪無補充發言）"}</p></div>; })}</div></details>
  </article>;
}

const verdictLabel: Record<string, string> = { big_win: "大勝", win: "勝", big_loss: "大敗", loss: "敗", abstain: "棄權", unverifiable: "無法驗證" };

function OutcomeSummary({ run, onSelect }: { run: Result; onSelect: (symbol: string, name?: string) => void }) {
  const symbols = run.conclusion.recommended_symbols ?? [];
  const rows = symbols.map((symbol) => {
    const open = run.day1_open_prices?.[symbol];
    const daily = run.daily_close_prices?.[symbol] ?? [];
    const close = [...daily].reverse().find((value): value is number => typeof value === "number") ?? run.day5_close_prices?.[symbol];
    const change = open && close ? (close / open - 1) * 100 : null;
    return { symbol, open, close, change };
  });
  return <section className="mt-5 rounded-xl border border-slate-800 bg-slate-950/50 p-4 text-slate-100">
    <div className="flex flex-wrap items-center justify-between gap-2"><div><h3 className="font-semibold text-amber-300">本場答案</h3><p className="mt-1 text-lg font-bold text-amber-100">{symbols.length ? symbols.map((s) => run.conclusion.symbol_names?.[s] ? `${s} ${run.conclusion.symbol_names[s]}` : s).join("、") : "本場沒有推薦標的"}</p></div><div className="text-right"><div className="text-xs text-slate-500">五日績效</div><span className={`mt-1 inline-block rounded-full bg-slate-800 px-3 py-1 text-sm font-bold ${run.verdict ? BAND_LABELS[run.verdict]?.cls ?? "text-slate-300" : "text-slate-300"}`}>{run.verdict ? verdictLabel[run.verdict] ?? run.verdict : run.verify_after_date ? `預計 ${new Date(`${run.verify_after_date}T00:00:00+08:00`).toLocaleDateString("zh-TW", { month: "numeric", day: "numeric" })} 對答案` : "等待驗證"}</span></div></div>
    {!!rows.length && <div className="mt-3 space-y-2">{rows.map((row) => {
      const changes = (run.daily_close_prices?.[row.symbol] ?? []).map((close) => row.open && close != null ? (close / row.open - 1) * 100 : null);
      const band = run.verdict ?? classifySymbolBand(changes.map((value) => value == null ? null : value / 100));
      const display = band ? BAND_LABELS[band] : { mark: "等待", cls: "text-slate-400" };
      return <button key={row.symbol} type="button" title="查看 K 線圖" onClick={() => onSelect(row.symbol, run.conclusion.symbol_names?.[row.symbol])} className="block w-full rounded-lg bg-slate-950/60 p-3 text-left text-sm transition hover:bg-slate-900"><b className={display.cls}>{row.symbol}{run.conclusion.symbol_names?.[row.symbol] ? ` ${run.conclusion.symbol_names[row.symbol]}` : ""} {display.mark}</b><div className="mt-1">{Array.from({ length: 5 }, (_, index) => { const value = changes[index]; return <span key={index}><span className={value == null ? "text-slate-500" : value >= 0 ? "text-up" : "text-down"}>{value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(1)}%`}</span>{index < 4 && <span className="text-slate-600">／</span>}</span>; })}</div></button>;
    })}</div>}
    <p className="mt-3 text-sm leading-6 text-slate-400">{run.verdict_reason || "完成五個交易日後，系統會在這裡顯示驗證結果。"}</p>
  </section>;
}
