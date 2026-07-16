import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CalendarDays, ShieldAlert, Users } from "lucide-react";
import { getPersonaIdentity } from "@/components/discussion/personaIdentity";
import { BAND_LABELS, classifySymbolBand } from "@/components/discussion/format/symbols";

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
  candidates?: Array<{ symbol?: string; strategy_score?: number; signal_type?: string }>;
  candidate_pool?: Array<{ symbol?: string; strategy_score?: number; signal_type?: string }>;
  verdict?: "big_win" | "win" | "big_loss" | "loss" | "unverifiable" | null;
  verdict_reason?: string | null;
  verified_at?: string | null;
  verify_after_date?: string | null;
  day1_open_prices?: Record<string, number> | null;
  day5_close_prices?: Record<string, number> | null;
  daily_close_prices?: Record<string, (number | null)[]> | null;
};
type DailyDay = { date: string; strategies: Record<string, Result[]> };
type Payload = { state: "disabled" | "empty" | "ready"; result: Result | null; strategies?: Record<string, Result[]>; days?: DailyDay[]; disclaimer: string };

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
  const [error, setError] = useState("");
  const [activeStrategy, setActiveStrategy] = useState("general");
  const [activeDate, setActiveDate] = useState("");

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
              {activeResults.length === 0 ? <StateCard icon={<CalendarDays className="h-6 w-6" />}>今日無符合安全條件的候選股。</StateCard> : <div className="space-y-6"><CandidatePool results={activeResults} />{activeResults.map((run) => <StrategyRun key={`${currentStrategy}-${run.sequence}`} run={run} />)}</div>}
            </section>

            <footer className="border-t border-slate-800 pt-6 text-xs leading-6 text-slate-500">{payload.disclaimer}</footer>
          </div>
        )}
      </div>
    </main>
  );
}

function StateCard({ children, icon, action }: { children: React.ReactNode; icon?: React.ReactNode; action?: () => void }) {
  return <div className="rounded-2xl border border-slate-800 bg-slate-900 p-8 text-center text-slate-300">{icon && <div className="mb-3 flex justify-center">{icon}</div>}<p>{children}</p>{action && <button onClick={action} className="mt-5 rounded-lg bg-success px-4 py-2 text-sm font-semibold text-slate-950">重試</button>}</div>;
}
function Info({ label, value }: { label: string; value: string }) {
  return <div className="rounded-xl bg-slate-950/50 p-4"><div className="text-xs text-slate-500">{label}</div><div className="mt-1 font-semibold">{value}</div></div>;
}

type PoolItem = { symbol?: string; strategy_score?: number; signal_type?: string };

export function dedupeBySymbol(items: PoolItem[]): PoolItem[] {
  const best = new Map<string, PoolItem>();
  for (const item of items) {
    if (!item.symbol) continue;
    const prev = best.get(item.symbol);
    if (!prev || (item.strategy_score ?? -Infinity) > (prev.strategy_score ?? -Infinity)) best.set(item.symbol, item);
  }
  return [...best.values()].sort((a, b) => (b.strategy_score ?? -Infinity) - (a.strategy_score ?? -Infinity));
}

function CandidatePool({ results }: { results: Result[] }) {
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
      <span key={c.symbol} className="rounded-full border border-slate-700 bg-slate-950/60 px-3 py-1 text-xs text-slate-300">
        {c.symbol}{typeof c.strategy_score === "number" && <> · {c.strategy_score.toFixed(1)}</>}
        {c.signal_type && signalNames[c.signal_type] && <span className="ml-1 text-amber-300">{signalNames[c.signal_type]}</span>}
      </span>
    ))}</div>
  </details>;
}

function StrategyRun({ run }: { run: Result }) {
  const warnings = qualityWarnings(run.conclusion.quality_signals);
  const signalCandidates = (run.candidates ?? []).filter((c) => c.symbol && c.signal_type && signalNames[c.signal_type]);
  return <article className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5 sm:p-7">
    {signalCandidates.length > 0 && <div className="mb-4 flex flex-wrap gap-2">{signalCandidates.map((c) => <span key={c.symbol} className="rounded-full border border-slate-700 bg-slate-950/60 px-3 py-1 text-xs text-slate-300">{c.symbol} · {signalNames[c.signal_type!]}</span>)}</div>}
    <OutcomeSummary run={run} />
    <div className="mt-4 flex flex-wrap gap-3">{(run.conclusion.recommended_symbols ?? []).map((symbol) => <div key={symbol} className="rounded-xl bg-amber-900/40 px-4 py-3 text-amber-100 ring-1 ring-amber-800/60"><div className="font-bold">{symbol}</div><div className="text-xs text-amber-200/70">{run.conclusion.symbol_names?.[symbol] ?? ""}</div></div>)}{!(run.conclusion.recommended_symbols?.length) && <p className="text-slate-400">本場沒有推薦標的</p>}</div>
    <p className="mt-5 whitespace-pre-wrap leading-7 text-slate-200">{run.conclusion.reasoning}</p>
    <div className="mt-5 grid gap-3 sm:grid-cols-2"><Info label="時間框架" value={horizon[run.conclusion.time_horizon ?? ""] ?? run.conclusion.time_horizon ?? "—"} /><Info label="共識度" value={typeof run.conclusion.consensus_score === "number" ? `${Math.round(run.conclusion.consensus_score * 100)}%` : "—"} /></div>
    {!!warnings.length && <div className="mt-5 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">{warnings.join("；")}</div>}
    <details className="mt-6"><summary className="flex cursor-pointer items-center gap-2 text-sm font-semibold text-slate-300"><Users className="h-4 w-4 text-success" />展開五輪完整發言</summary><div className="mt-4 space-y-3">{run.turns.map((turn) => { const identity = getPersonaIdentity(turn.persona_id, turn.persona_name); return <div key={`${turn.round}-${turn.turn_index}`} className="rounded-xl border border-l-[3px] border-slate-800 bg-slate-950/60 p-4" style={{ borderLeftColor: identity.accentColor }}><div className="flex items-center justify-between gap-3 text-sm"><b style={{ color: identity.accentColor }}>{turn.persona_name}</b><span className={`rounded-full border px-2 py-0.5 text-xs ${stanceTone[turn.stance] ?? "border-slate-700 bg-slate-800 text-slate-400"}`}>第 {turn.round} 輪 · {stance[turn.stance] ?? turn.stance}</span></div><p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-300">{turn.content || "（本輪無補充發言）"}</p></div>; })}</div></details>
  </article>;
}

const verdictLabel: Record<string, string> = { big_win: "大勝", win: "勝", big_loss: "大敗", loss: "敗", unverifiable: "無法驗證" };

function OutcomeSummary({ run }: { run: Result }) {
  const symbols = run.conclusion.recommended_symbols ?? [];
  const rows = symbols.map((symbol) => {
    const open = run.day1_open_prices?.[symbol];
    const daily = run.daily_close_prices?.[symbol] ?? [];
    const close = [...daily].reverse().find((value): value is number => typeof value === "number") ?? run.day5_close_prices?.[symbol];
    const change = open && close ? (close / open - 1) * 100 : null;
    return { symbol, open, close, change };
  });
  return <section className="mt-5 rounded-xl border border-slate-800 bg-slate-950/50 p-4 text-slate-100">
    <div className="flex flex-wrap items-center justify-between gap-2"><div><h3 className="font-semibold text-amber-300">本場答案</h3><p className="mt-1 text-lg font-bold text-amber-100">{symbols.length ? symbols.join("、") : "本場沒有推薦標的"}</p></div><div className="text-right"><div className="text-xs text-slate-500">五日績效</div><span className={`mt-1 inline-block rounded-full bg-slate-800 px-3 py-1 text-sm font-bold ${run.verdict ? BAND_LABELS[run.verdict]?.cls ?? "text-slate-300" : "text-slate-300"}`}>{run.verdict ? verdictLabel[run.verdict] ?? run.verdict : run.verify_after_date ? `預計 ${new Date(`${run.verify_after_date}T00:00:00+08:00`).toLocaleDateString("zh-TW", { month: "numeric", day: "numeric" })} 對答案` : "等待驗證"}</span></div></div>
    {!!rows.length && <div className="mt-3 space-y-2">{rows.map((row) => {
      const changes = (run.daily_close_prices?.[row.symbol] ?? []).map((close) => row.open && close != null ? (close / row.open - 1) * 100 : null);
      const band = run.verdict ?? classifySymbolBand(changes.map((value) => value == null ? null : value / 100));
      const display = band ? BAND_LABELS[band] : { mark: "等待", cls: "text-slate-400" };
      return <div key={row.symbol} className="rounded-lg bg-slate-950/60 p-3 text-sm"><b className={display.cls}>{row.symbol} {display.mark}</b><div className="mt-1">{Array.from({ length: 5 }, (_, index) => { const value = changes[index]; return <span key={index}><span className={value == null ? "text-slate-500" : value >= 0 ? "text-up" : "text-down"}>{value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(1)}%`}</span>{index < 4 && <span className="text-slate-600">／</span>}</span>; })}</div></div>;
    })}</div>}
    <p className="mt-3 text-sm leading-6 text-slate-400">{run.verdict_reason || "完成五個交易日後，系統會在這裡顯示驗證結果。"}</p>
  </section>;
}
