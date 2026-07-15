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
};
type Payload = { state: "disabled" | "empty" | "ready"; result: Result | null; disclaimer: string };

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
  const conclusion = result?.conclusion;
  const warnings = qualityWarnings(conclusion?.quality_signals);
  const rounds = result ? Array.from(new Set(result.turns.map((t) => t.round))).sort((a, b) => a - b) : [];

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

        {payload?.state === "ready" && result && conclusion && (
          <div className="space-y-8">
            <section>
              <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                <span className="rounded-full bg-success/10 px-3 py-1 font-semibold text-success">{result.market}</span>
                <span className="flex items-center gap-1"><CalendarDays className="h-3.5 w-3.5" />{new Date(result.created_at).toLocaleDateString("zh-TW")}</span>
                {result.captured_session?.session_date && <span className="flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" />資料截至 {result.captured_session.session_date}</span>}
              </div>
              <h2 className="text-xl font-semibold sm:text-2xl">{result.topic}</h2>
            </section>

            <section className="rounded-2xl border border-success/20 bg-gradient-to-br from-success/10 to-slate-900 p-5 sm:p-8">
              <p className="mb-4 text-xs font-bold tracking-widest text-success">原始結論</p>
              <div className="flex flex-wrap gap-3">
                {(conclusion.recommended_symbols ?? []).map((symbol) => (
                  <div key={symbol} className="rounded-xl bg-slate-950/70 px-4 py-3">
                    <div className="text-lg font-bold">{symbol}</div>
                    {conclusion.symbol_names?.[symbol] && <div className="text-xs text-slate-400">{conclusion.symbol_names[symbol]}</div>}
                    {conclusion.recommendations?.find((r) => r.symbol === symbol) && <div className="mt-1 text-xs text-success">信心 {Math.round((conclusion.recommendations.find((r) => r.symbol === symbol)?.calibrated_confidence ?? conclusion.recommendations.find((r) => r.symbol === symbol)!.confidence) * 100)}%</div>}
                  </div>
                ))}
                {!(conclusion.recommended_symbols?.length) && <p className="text-slate-300">本次沒有推薦標的</p>}
              </div>
              <p className="mt-6 whitespace-pre-wrap leading-7 text-slate-200">{conclusion.reasoning}</p>
              <div className="mt-6 grid gap-4 sm:grid-cols-2">
                <Info label="時間框架" value={horizon[conclusion.time_horizon ?? ""] ?? conclusion.time_horizon ?? "—"} />
                <Info label="共識度" value={typeof conclusion.consensus_score === "number" ? `${Math.round(conclusion.consensus_score * 100)}%` : "—"} />
              </div>
              {!!conclusion.risks?.length && <div className="mt-6"><h3 className="mb-2 font-semibold text-amber-300">主要風險</h3><ul className="space-y-2 text-sm text-slate-300">{conclusion.risks.map((risk, i) => <li key={i} className="flex gap-2"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />{risk}</li>)}</ul></div>}
              {!!warnings.length && <div className="mt-6 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4"><h3 className="font-semibold text-amber-300">品質警示</h3><ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-amber-100/80">{warnings.map((w) => <li key={w}>{w}</li>)}</ul></div>}
            </section>

            <section>
              <div className="mb-5 flex items-center gap-2"><Users className="h-5 w-5 text-success" /><h2 className="text-xl font-semibold">五輪完整發言</h2></div>
              <div className="space-y-7">{rounds.map((round) => <div key={round}><h3 className="mb-3 text-sm font-bold tracking-wider text-slate-400">ROUND {round}</h3><div className="space-y-3">{result.turns.filter((t) => t.round === round).map((turn) => <article key={`${turn.round}-${turn.turn_index}`} className="rounded-xl border border-slate-800 bg-slate-900/70 p-4 sm:p-5"><div className="mb-3 flex items-center justify-between gap-3"><span className="font-semibold text-slate-100">{turn.persona_name}</span><span className="rounded-full bg-slate-800 px-2.5 py-1 text-xs text-slate-400">{stance[turn.stance] ?? turn.stance}</span></div><p className="whitespace-pre-wrap text-sm leading-7 text-slate-300 sm:text-base">{turn.content || "（本輪無補充發言）"}</p></article>)}</div></div>)}</div>
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

