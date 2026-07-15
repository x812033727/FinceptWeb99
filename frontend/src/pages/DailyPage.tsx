import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CalendarDays, Clock3, RefreshCw, ShieldAlert, Users } from "lucide-react";

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
  candidates?: Array<{ symbol?: string; strategy_score?: number }>;
};
type Payload = { state: "disabled" | "empty" | "ready"; result: Result | null; strategies?: Record<string, Result[]>; disclaimer: string };

const strategyNames: Record<string, string> = { general: "綜合選股", chip_momentum: "籌碼動能", quality_growth: "品質成長", breakout: "突破追價", oversold_reversal: "超跌反轉" };

const horizon: Record<string, string> = {
  short_term: "短期", medium_term: "中期", long_term: "長期",
};
const stance: Record<string, string> = { agree: "贊同", dissent: "異議", supplement: "補充" };

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
  const [refreshing, setRefreshing] = useState(false);
  const [activeStrategy, setActiveStrategy] = useState("general");

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const res = await fetch("/api/public/daily", { credentials: "omit", headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setPayload(await res.json() as Payload);
      setError("");
    } catch {
      setError("目前無法取得每日圓桌結果，請稍後再試。");
    } finally {
      setRefreshing(false);
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
  const strategies = payload?.strategies ?? {};
  const activeResults = strategies[activeStrategy] ?? [];

  return (
    <main className="min-h-screen bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-5xl px-4 py-10 sm:px-6 sm:py-16">
        <header className="mb-10 border-b border-slate-800 pb-8">
          <div className="mb-3 flex items-center justify-between gap-4">
            <p className="text-sm font-semibold tracking-[0.22em] text-success">FINCEPT DAILY</p>
            <button onClick={() => void load()} disabled={refreshing} aria-label="重新整理" className="rounded-full border border-slate-700 p-2 text-slate-400 hover:text-white disabled:opacity-50">
              <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
            </button>
          </div>
          <h1 className="text-3xl font-bold tracking-tight sm:text-5xl">每日投資圓桌</h1>
          <p className="mt-3 max-w-2xl text-slate-400">最新一次成功的自動討論，完整公開原始結論與五輪專家發言。</p>
        </header>

        {!payload && !error && <StateCard>正在取得最新結果…</StateCard>}
        {error && <StateCard icon={<AlertTriangle className="h-6 w-6 text-amber-400" />} action={load}>{error}</StateCard>}
        {payload?.state === "disabled" && <StateCard icon={<ShieldAlert className="h-6 w-6" />}>每日公開結果尚未設定。</StateCard>}
        {payload?.state === "empty" && <StateCard icon={<CalendarDays className="h-6 w-6" />}>目前尚無可公開的成功討論結果。</StateCard>}

        {payload?.state === "ready" && result && (
          <div className="space-y-8">
            <nav role="tablist" aria-label="每日選股策略" className="flex gap-2 overflow-x-auto border-b border-slate-800 pb-3">
              {Object.entries(strategyNames).map(([key, name]) => <button key={key} role="tab" aria-selected={activeStrategy === key} onClick={() => setActiveStrategy(key)} className={`shrink-0 rounded-xl px-4 py-2.5 text-sm font-semibold transition ${activeStrategy === key ? "bg-success text-slate-950" : "bg-slate-900 text-slate-400 hover:text-white"}`}>
                {name}<span className={`ml-2 rounded-full px-2 py-0.5 text-xs ${activeStrategy === key ? "bg-slate-950/15" : "bg-slate-800"}`}>{(strategies[key] ?? []).length}</span>
              </button>)}
            </nav>

            <section role="tabpanel" aria-label={strategyNames[activeStrategy]}>
              <div className="mb-5 flex items-center justify-between"><h2 className="text-2xl font-semibold">{strategyNames[activeStrategy]}</h2><span className="text-sm text-slate-400">完成 {activeResults.length} 場</span></div>
              {activeResults.length === 0 ? <StateCard icon={<CalendarDays className="h-6 w-6" />}>今日無符合安全條件的候選股。</StateCard> : <div className="space-y-6">{activeResults.map((run) => <StrategyRun key={`${activeStrategy}-${run.sequence}`} run={run} />)}</div>}
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

function StrategyRun({ run }: { run: Result }) {
  const warnings = qualityWarnings(run.conclusion.quality_signals);
  const candidates = (run.candidates ?? []).map((item) => item.symbol).filter(Boolean).join("、") || "綜合候選池";
  return <article className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5 sm:p-7">
    <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400"><span className="rounded-full bg-success/10 px-3 py-1 font-semibold text-success">第 {run.sequence ?? 1} 場</span><span>{run.market}</span><span className="flex items-center gap-1"><CalendarDays className="h-3.5 w-3.5" />{new Date(run.created_at).toLocaleDateString("zh-TW")}</span>{run.captured_session?.session_date && <span className="flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" />資料截至 {run.captured_session.session_date}</span>}</div>
    <p className="mt-3 text-sm text-slate-400">候選：{candidates}</p>
    <div className="mt-4 flex flex-wrap gap-3">{(run.conclusion.recommended_symbols ?? []).map((symbol) => <div key={symbol} className="rounded-xl bg-slate-950 px-4 py-3"><div className="font-bold">{symbol}</div><div className="text-xs text-slate-400">{run.conclusion.symbol_names?.[symbol] ?? ""}</div></div>)}{!(run.conclusion.recommended_symbols?.length) && <p className="text-slate-400">本場沒有推薦標的</p>}</div>
    <p className="mt-5 whitespace-pre-wrap leading-7 text-slate-200">{run.conclusion.reasoning}</p>
    <div className="mt-5 grid gap-3 sm:grid-cols-2"><Info label="時間框架" value={horizon[run.conclusion.time_horizon ?? ""] ?? run.conclusion.time_horizon ?? "—"} /><Info label="共識度" value={typeof run.conclusion.consensus_score === "number" ? `${Math.round(run.conclusion.consensus_score * 100)}%` : "—"} /></div>
    {!!warnings.length && <div className="mt-5 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">{warnings.join("；")}</div>}
    <details className="mt-6"><summary className="flex cursor-pointer items-center gap-2 text-sm font-semibold text-slate-300"><Users className="h-4 w-4 text-success" />展開五輪完整發言</summary><div className="mt-4 space-y-3">{run.turns.map((turn) => <div key={`${turn.round}-${turn.turn_index}`} className="rounded-xl border border-slate-800 bg-slate-950/60 p-4"><div className="flex justify-between gap-3 text-sm"><b>{turn.persona_name}</b><span className="text-slate-500">第 {turn.round} 輪 · {stance[turn.stance] ?? turn.stance}</span></div><p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-300">{turn.content || "（本輪無補充發言）"}</p></div>)}</div></details>
  </article>;
}
