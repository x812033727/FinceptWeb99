import { useEffect, useState, useRef, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from "recharts";
import { useTranslation } from "react-i18next";
import { useAuthStore } from "@/store/authStore";
import { notifyRateLimited } from "@/lib/api";
import {
  usePortfolios,
  usePortfolioDetail,
  useCreatePortfolio,
  useDeletePortfolio,
  useUpdatePortfolio,
  useAddTransaction,
  useUpdateTransaction,
  useDeleteTransaction,
  useOptimise,
} from "@/hooks/usePortfolio";
import HoldingsTable from "@/components/portfolio/HoldingsTable";
import AllocationPie from "@/components/portfolio/AllocationPie";
import api from "@/lib/api";

// ── Auto trade-day FX rate ────────────────────────────────────────
//
// When the user picks a foreign-currency stock and a transaction date,
// the form auto-fills the FX field with the rate FRED reported on that
// trade day so cost basis is denominated correctly. The user can still
// override by editing the field; once they do, `userPinnedFx` flips to
// true and we stop overwriting their value.
async function fetchSuggestedFxRate(
  portfolioId: string,
  market: string,
  txDate: string,
): Promise<number | null> {
  try {
    const res = await api.get<{ fx_rate: number }>(
      `/portfolio/${portfolioId}/fx-rate?market=${market}&tx_date=${txDate}`,
    );
    return res.data.fx_rate;
  } catch {
    return null;
  }
}

// ── Add Transaction form ──────────────────────────────────────────
function AddTransactionForm({ portfolioId, onClose }: { portfolioId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const add = useAddTransaction(portfolioId);
  const [form, setForm] = useState({
    symbol: "", market: "US", tx_type: "buy",
    quantity: "", price: "", fx_rate: "",
    tx_date: new Date().toISOString().slice(0, 10), notes: "",
  });
  const [userPinnedFx, setUserPinnedFx] = useState(false);
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    if (k === "fx_rate") setUserPinnedFx(true);
    setForm((f) => ({ ...f, [k]: e.target.value }));
  };

  useEffect(() => {
    if (userPinnedFx) return;
    let cancelled = false;
    (async () => {
      const rate = await fetchSuggestedFxRate(portfolioId, form.market, form.tx_date);
      if (cancelled || rate == null) return;
      setForm((f) => ({ ...f, fx_rate: String(rate) }));
    })();
    return () => { cancelled = true; };
  }, [portfolioId, form.market, form.tx_date, userPinnedFx]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    // Empty string → send null so the backend auto-stamps the historical
    // rate. Sending 0 or NaN would be rejected by Pydantic.
    const fx = form.fx_rate.trim() === "" ? null : parseFloat(form.fx_rate);
    await add.mutateAsync({
      symbol: form.symbol.toUpperCase(),
      market: form.market,
      tx_type: form.tx_type,
      quantity: parseFloat(form.quantity),
      price: parseFloat(form.price),
      fx_rate: fx,
      tx_date: form.tx_date,
      notes: form.notes || undefined,
    } as any);
    onClose();
  }

  const input = "w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
  const label = "block text-xs text-muted-foreground mb-1";

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <form onSubmit={submit} className="bg-card border border-border rounded-lg p-6 w-full max-w-md space-y-4">
        <h3 className="text-foreground font-semibold">{t("portfolio.transactions.add")}</h3>
        <div className="grid grid-cols-2 gap-3">
          <div><label className={label}>{t("alerts.symbol")}</label><input required className={input} value={form.symbol} onChange={set("symbol")} placeholder="AAPL / 2330" /></div>
          <div><label className={label}>{t("alerts.market")}</label>
            <select className={input} value={form.market} onChange={set("market")}>
              <option value="US">US</option><option value="TW">TW</option><option value="CRYPTO">CRYPTO</option>
            </select>
          </div>
          <div><label className={label}>{t("portfolio.transactions.type")}</label>
            <select className={input} value={form.tx_type} onChange={set("tx_type")}>
              <option value="buy">{t("portfolio.transactions.buy")}</option><option value="sell">{t("portfolio.transactions.sell")}</option><option value="dividend">Dividend</option>
            </select>
          </div>
          <div><label className={label}>{t("portfolio.transactions.executed_at")}</label><input type="date" required className={input} value={form.tx_date} onChange={set("tx_date")} /></div>
          <div><label className={label}>{t("portfolio.transactions.qty")}</label><input type="number" required min="0" step="any" className={input} value={form.quantity} onChange={set("quantity")} /></div>
          <div><label className={label}>{t("portfolio.transactions.price")}</label><input type="number" required min="0" step="any" className={input} value={form.price} onChange={set("price")} /></div>
          <div><label className={label}>FX Rate</label><input type="number" min="0" step="any" placeholder="auto" className={input} value={form.fx_rate} onChange={set("fx_rate")} /></div>
          <div><label className={label}>Notes</label><input className={input} value={form.notes} onChange={set("notes")} /></div>
        </div>
        <div className="flex gap-3 justify-end">
          <button type="button" onClick={onClose} className="px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground">{t("common.cancel")}</button>
          <button type="submit" disabled={add.isPending} className="px-4 py-1.5 text-sm bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-50">
            {add.isPending ? t("common.saving") : t("common.add")}
          </button>
        </div>
      </form>
    </div>
  );
}

// ── Create Portfolio modal ────────────────────────────────────────
function CreatePortfolioModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const create = useCreatePortfolio();
  const [name, setName] = useState("");
  const [currency, setCurrency] = useState("USD");

  async function submit(e: FormEvent) {
    e.preventDefault();
    await create.mutateAsync({ name, currency });
    onClose();
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <form onSubmit={submit} className="bg-card border border-border rounded-lg p-6 w-full max-w-sm space-y-4">
        <h3 className="text-foreground font-semibold">{t("portfolio.new_portfolio")}</h3>
        <div><label className="block text-xs text-muted-foreground mb-1">{t("portfolio.portfolio_name")}</label>
          <input required className="w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring" value={name} onChange={(e) => setName(e.target.value)} /></div>
        <div><label className="block text-xs text-muted-foreground mb-1">{t("portfolio.currency")}</label>
          <select className="w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground" value={currency} onChange={(e) => setCurrency(e.target.value)}>
            <option value="USD">USD</option><option value="TWD">TWD</option>
          </select></div>
        <div className="flex gap-3 justify-end">
          <button type="button" onClick={onClose} className="px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground">{t("common.cancel")}</button>
          <button type="submit" disabled={create.isPending} className="px-4 py-1.5 text-sm bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-50">{t("portfolio.create")}</button>
        </div>
      </form>
    </div>
  );
}

// ── Edit Portfolio modal (rename + change currency) ──────────────
function EditPortfolioModal({
  portfolioId, currentName, currentCurrency, onClose,
}: { portfolioId: string; currentName: string; currentCurrency: string; onClose: () => void }) {
  const { t } = useTranslation();
  const update = useUpdatePortfolio(portfolioId);
  const [name, setName] = useState(currentName);
  const [currency, setCurrency] = useState(currentCurrency);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const patch: { name?: string; currency?: string } = {};
    if (name.trim() && name !== currentName) patch.name = name.trim();
    if (currency !== currentCurrency) patch.currency = currency;
    if (Object.keys(patch).length === 0) { onClose(); return; }
    await update.mutateAsync(patch);
    onClose();
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <form onSubmit={submit} className="bg-card border border-border rounded-lg p-6 w-full max-w-sm space-y-4">
        <h3 className="text-foreground font-semibold">{t("portfolio.edit_title")}</h3>
        <div>
          <label className="block text-xs text-muted-foreground mb-1">{t("portfolio.portfolio_name")}</label>
          <input
            required
            className="w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div>
          <label className="block text-xs text-muted-foreground mb-1">{t("portfolio.currency")}</label>
          <select
            className="w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground"
            value={currency}
            onChange={(e) => setCurrency(e.target.value)}
          >
            <option value="USD">USD</option>
            <option value="TWD">TWD</option>
          </select>
        </div>
        <div className="flex gap-3 justify-end">
          <button type="button" onClick={onClose} className="px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground">{t("common.cancel")}</button>
          <button type="submit" disabled={update.isPending} className="px-4 py-1.5 text-sm bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-50">
            {update.isPending ? t("common.saving") : t("common.save")}
          </button>
        </div>
      </form>
    </div>
  );
}


// ── Edit Transaction modal ───────────────────────────────────────
function EditTransactionModal({
  portfolioId, tx, onClose,
}: { portfolioId: string; tx: TransactionRow; onClose: () => void }) {
  const { t } = useTranslation();
  const update = useUpdateTransaction(portfolioId);
  const [form, setForm] = useState({
    symbol: tx.symbol,
    market: tx.market,
    tx_type: tx.tx_type,
    quantity: String(tx.quantity),
    price: String(tx.price),
    fx_rate: String(tx.fx_rate),
    tx_date: tx.tx_date,
    notes: tx.notes ?? "",
  });
  // Treat the existing tx.fx_rate as user-pinned — don't silently rewrite
  // it with the suggested rate unless the user explicitly changes the
  // market or tx_date (handled below).
  const [userPinnedFx, setUserPinnedFx] = useState(true);
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    if (k === "fx_rate") setUserPinnedFx(true);
    if (k === "market" || k === "tx_date") setUserPinnedFx(false);
    setForm((f) => ({ ...f, [k]: e.target.value }));
  };

  useEffect(() => {
    if (userPinnedFx) return;
    let cancelled = false;
    (async () => {
      const rate = await fetchSuggestedFxRate(portfolioId, form.market, form.tx_date);
      if (cancelled || rate == null) return;
      setForm((f) => ({ ...f, fx_rate: String(rate) }));
    })();
    return () => { cancelled = true; };
  }, [portfolioId, form.market, form.tx_date, userPinnedFx]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    await update.mutateAsync({
      txId: tx.id,
      patch: {
        symbol: form.symbol.toUpperCase(),
        market: form.market,
        tx_type: form.tx_type,
        quantity: parseFloat(form.quantity),
        price: parseFloat(form.price),
        fx_rate: parseFloat(form.fx_rate),
        tx_date: form.tx_date,
        notes: form.notes || undefined,
      },
    });
    onClose();
  }

  const input = "w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
  const label = "block text-xs text-muted-foreground mb-1";

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <form onSubmit={submit} className="bg-card border border-border rounded-lg p-6 w-full max-w-md space-y-4">
        <h3 className="text-foreground font-semibold">{t("portfolio.transactions.edit")}</h3>
        <div className="grid grid-cols-2 gap-3">
          <div><label className={label}>{t("alerts.symbol")}</label><input required className={input} value={form.symbol} onChange={set("symbol")} /></div>
          <div><label className={label}>{t("alerts.market")}</label>
            <select className={input} value={form.market} onChange={set("market")}>
              <option value="US">US</option><option value="TW">TW</option><option value="CRYPTO">CRYPTO</option>
            </select>
          </div>
          <div><label className={label}>{t("portfolio.transactions.type")}</label>
            <select className={input} value={form.tx_type} onChange={set("tx_type")}>
              <option value="buy">{t("portfolio.transactions.buy")}</option>
              <option value="sell">{t("portfolio.transactions.sell")}</option>
              <option value="dividend">Dividend</option>
            </select>
          </div>
          <div><label className={label}>{t("portfolio.transactions.executed_at")}</label><input type="date" required className={input} value={form.tx_date} onChange={set("tx_date")} /></div>
          <div><label className={label}>{t("portfolio.transactions.qty")}</label><input type="number" required min="0" step="any" className={input} value={form.quantity} onChange={set("quantity")} /></div>
          <div><label className={label}>{t("portfolio.transactions.price")}</label><input type="number" required min="0" step="any" className={input} value={form.price} onChange={set("price")} /></div>
          <div><label className={label}>FX Rate</label><input type="number" min="0" step="any" className={input} value={form.fx_rate} onChange={set("fx_rate")} /></div>
          <div><label className={label}>Notes</label><input className={input} value={form.notes} onChange={set("notes")} /></div>
        </div>
        <div className="flex gap-3 justify-end">
          <button type="button" onClick={onClose} className="px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground">{t("common.cancel")}</button>
          <button type="submit" disabled={update.isPending} className="px-4 py-1.5 text-sm bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-50">
            {update.isPending ? t("common.saving") : t("common.save")}
          </button>
        </div>
      </form>
    </div>
  );
}


// ── Expert evaluation card ───────────────────────────────────────
//
// In-place AI evaluation of the active portfolio. Lets the user pick any of
// the 19 personas (Buffett / Lynch / Dalio / quant / functional CFA …) and
// streams the response inline rather than navigating to /ai. Reuses the same
// /api/ai/chat SSE endpoint as the AI page; tool calls show inline so
// claude_research's research workflow stays visible.
//
// Quota: each evaluation = 1 AI request, identical to a normal chat turn.

interface AgentInfo {
  id: string;
  name: string;
  description: string;
  default_provider: string;
}

interface InlineToolCall {
  id: string;
  name: string;
  status: "running" | "done" | "error";
  result?: string;
  isError?: boolean;
}

const EVAL_PROMPT_TEMPLATE = `請以您的投資哲學評估這個投資組合：

1. **整體評估**：在您的框架下這個組合有哪些強項與弱點？
2. **個別持倉**：哪些值得繼續持有、哪些應考慮賣出、有什麼遺漏的標的應該加入？
3. **風險與集中度**：是否過度集中於某個產業、地區或因子？最大的潛在風險為何？
4. **具體建議**：給出 2-3 個可立即執行的調整建議（含理由與優先順序）。

請以 3-5 個項目符號作結，總結最關鍵的洞見。`;

function ExpertEvaluationCard({ detail }: { portfolioId: string; detail: any }) {
  const { t } = useTranslation();
  const token = useAuthStore((s) => s.token);
  const navigate = useNavigate();

  const { data: agents = [] } = useQuery<AgentInfo[]>({
    queryKey: ["ai-agents"],
    queryFn: () => api.get<AgentInfo[]>("/ai/agents").then((r) => r.data),
    staleTime: 5 * 60_000,
  });

  const [selectedAgent, setSelectedAgent] = useState("portfolio_advisor");
  const [streaming, setStreaming] = useState(false);
  const [response, setResponse] = useState("");
  const [toolCalls, setToolCalls] = useState<InlineToolCall[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [hasResult, setHasResult] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const activeAgent = agents.find((a) => a.id === selectedAgent);

  async function evaluate() {
    if (streaming) return;
    setStreaming(true);
    setResponse("");
    setToolCalls([]);
    setError(null);
    setHasResult(true);

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let assembled = "";

    try {
      const resp = await fetch("/api/ai/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          agent_id: selectedAgent,
          messages: [{ role: "user", content: EVAL_PROMPT_TEMPLATE }],
          context: { portfolio: detail ?? null },
        }),
        signal: ctrl.signal,
      });

      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 429) {
          const retryAfter = Number(resp.headers.get("retry-after")) || undefined;
          notifyRateLimited(data.detail, retryAfter);
        }
        throw new Error(data.detail ?? `HTTP ${resp.status}`);
      }

      const reader = resp.body!.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n\n");
        buf = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6).trim();
          if (payload === "[DONE]") break;
          try {
            const obj = JSON.parse(payload);
            if (obj.error) { setError(obj.error); break; }
            if (obj.delta) {
              assembled += obj.delta;
              setResponse(assembled);
            }
            if (obj.tool_call) {
              setToolCalls((prev) => [...prev, {
                id: obj.tool_call.id,
                name: obj.tool_call.name,
                status: "running",
              }]);
            }
            if (obj.tool_result) {
              setToolCalls((prev) => prev.map((tc) =>
                tc.id === obj.tool_result.id
                  ? { ...tc, result: obj.tool_result.summary,
                      isError: obj.tool_result.is_error,
                      status: obj.tool_result.is_error ? "error" : "done" }
                  : tc,
              ));
            }
          } catch { /* ignore malformed */ }
        }
      }
    } catch (e: unknown) {
      if ((e as Error).name !== "AbortError") {
        setError((e as Error).message);
      }
    } finally {
      setStreaming(false);
    }
  }

  function stop() {
    abortRef.current?.abort();
  }

  function openInAIPage() {
    navigate("/ai", {
      state: {
        agentId: selectedAgent,
        initialMessage: EVAL_PROMPT_TEMPLATE,
        context: { portfolio: detail ?? null },
      },
    });
  }

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-3">
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <div>
          <h2 className="text-foreground font-medium">🤖 {t("portfolio.expert_eval.title")}</h2>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t("portfolio.expert_eval.subtitle")}
          </p>
        </div>
        <button
          onClick={openInAIPage}
          className="text-xs text-primary hover:underline"
        >
          {t("portfolio.expert_eval.open_in_ai")} →
        </button>
      </div>

      <div className="flex gap-2 items-stretch flex-wrap">
        <select
          value={selectedAgent}
          onChange={(e) => setSelectedAgent(e.target.value)}
          disabled={streaming}
          className="flex-1 min-w-[200px] bg-background border border-border rounded px-3 py-1.5 text-sm text-foreground"
        >
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}{a.default_provider ? ` · ${a.default_provider}` : ""}
            </option>
          ))}
        </select>
        {streaming ? (
          <button
            onClick={stop}
            className="px-4 py-1.5 rounded bg-red-900/30 border border-red-800 text-red-400 text-sm hover:bg-red-900/50"
          >
            {t("ai.stop")}
          </button>
        ) : (
          <button
            onClick={evaluate}
            disabled={!detail}
            className="px-4 py-1.5 rounded bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 disabled:opacity-50"
          >
            {hasResult ? t("portfolio.expert_eval.re_evaluate") : t("portfolio.expert_eval.evaluate")}
          </button>
        )}
      </div>

      {activeAgent && (
        <p className="text-xs text-muted-foreground">{activeAgent.description}</p>
      )}

      {hasResult && (
        <div className="space-y-2 mt-2">
          {toolCalls.length > 0 && (
            <div className="space-y-1">
              {toolCalls.map((tc) => (
                <details key={tc.id} className="text-xs border border-border/60 rounded bg-muted/20 p-2">
                  <summary className="cursor-pointer flex items-center gap-2 list-none">
                    <span className={`inline-block w-2 h-2 rounded-full ${
                      tc.status === "running" ? "bg-amber-400 animate-pulse"
                      : tc.status === "error" ? "bg-red-500"
                      : "bg-green-500"
                    }`} />
                    <span className="font-mono text-amber-300">{tc.name}</span>
                    <span className="text-muted-foreground">
                      {tc.status === "running" ? t("ai.tool.calling") : tc.status === "error" ? t("ai.tool.failed") : t("ai.tool.done")}
                    </span>
                  </summary>
                  {tc.result && (
                    <pre className="mt-1 bg-background/60 border border-border rounded p-2 overflow-auto max-h-48 text-foreground/80 whitespace-pre-wrap text-[10px]">
                      {tc.result}
                    </pre>
                  )}
                </details>
              ))}
            </div>
          )}

          <div className="bg-background/40 border border-border rounded p-3 text-sm text-foreground whitespace-pre-wrap leading-relaxed min-h-[3rem]">
            {response || (streaming ? <span className="text-muted-foreground animate-pulse">{t("common.loading")}</span> : "")}
            {streaming && response && (
              <span className="inline-block w-1.5 h-3.5 bg-current ml-0.5 animate-pulse align-middle" />
            )}
          </div>

          {error && (
            <p className="text-xs text-red-400 bg-red-950/20 border border-red-900/40 rounded px-3 py-2">{error}</p>
          )}

          <p className="text-[10px] text-muted-foreground">
            {t("ai.disclaimer")} · {t("portfolio.expert_eval.uses_quota", { count: 1 })}
          </p>
        </div>
      )}
    </div>
  );
}


// ── Performance chart ─────────────────────────────────────────────

const PERIOD_DAYS: Record<string, number> = { "1M": 30, "3M": 90, "6M": 180, "1Y": 365 };
const PERIOD_TO_YFINANCE: Record<string, string> = { "1M": "1mo", "3M": "3mo", "6M": "6mo", "1Y": "1y" };

function normalize(points: { date: string; value: number }[]): { date: string; indexed: number }[] {
  if (!points.length) return [];
  const base = points[0].value;
  if (base === 0) return [];
  return points.map((p) => ({ date: p.date.slice(5), indexed: parseFloat(((p.value / base) * 100).toFixed(2)) }));
}

function PerformanceChart({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const [period, setPeriod] = useState("3M");
  const days = PERIOD_DAYS[period];
  const yfinancePeriod = PERIOD_TO_YFINANCE[period];

  const { data: perfData = [], isLoading: perfLoading } = useQuery<{ date: string; value: number }[]>({
    queryKey: ["portfolio-performance", portfolioId, days],
    queryFn: () =>
      api.get(`/portfolio/${portfolioId}/performance?days=${days}`).then((r) => r.data),
    staleTime: 3_600_000,
  });

  const { data: spyHistory = [] } = useQuery<{ date: string; close: number }[]>({
    queryKey: ["spy-history", yfinancePeriod],
    queryFn: () =>
      api.get(`/us/history/SPY?period=${yfinancePeriod}&interval=1d`).then((r) => r.data),
    staleTime: 3_600_000,
  });

  const portfolioIndexed = normalize(perfData);

  const spyIndexed = (() => {
    if (!spyHistory.length) return [];
    const base = spyHistory[0].close;
    if (base === 0) return [];
    return spyHistory.map((p) => ({ date: p.date?.slice(0, 10).slice(5), spy: parseFloat(((p.close / base) * 100).toFixed(2)) }));
  })();

  // Merge on date key
  const merged: Record<string, { date: string; portfolio?: number; spy?: number }> = {};
  for (const p of portfolioIndexed) merged[p.date] = { date: p.date, portfolio: p.indexed };
  for (const s of spyIndexed) {
    if (merged[s.date]) merged[s.date].spy = s.spy;
    else merged[s.date] = { date: s.date, spy: s.spy };
  }
  const chartData = Object.values(merged).sort((a, b) => a.date.localeCompare(b.date));

  const hasPerfData = portfolioIndexed.length > 0;

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-foreground font-medium">{t("portfolio.summary.performance")}</h2>
        </div>
        <div className="flex gap-1">
          {Object.keys(PERIOD_DAYS).map((p) => (
            <button
              key={p}
              onClick={() => setPeriod(p)}
              className={`px-2.5 py-1 text-xs rounded transition-colors ${
                period === p
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {p}
            </button>
          ))}
        </div>
      </div>

      {perfLoading ? (
        <div className="h-[200px] flex items-center justify-center text-muted-foreground text-sm animate-pulse">
          {t("common.loading")}
        </div>
      ) : !hasPerfData ? (
        <div className="h-[200px] flex items-center justify-center text-muted-foreground text-sm">
          {t("common.no_data")}
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={chartData} margin={{ left: 0, right: 8, top: 4, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis
              dataKey="date"
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              width={44}
              tickFormatter={(v) => v.toFixed(0)}
              domain={["auto", "auto"]}
            />
            <Tooltip
              contentStyle={{
                background: "hsl(var(--card))",
                border: "1px solid hsl(var(--border))",
                fontSize: 11,
              }}
              formatter={(v: number, name: string) => [`${v.toFixed(2)}`, name]}
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <Line
              type="monotone"
              dataKey="portfolio"
              name="Portfolio"
              stroke="hsl(var(--primary))"
              strokeWidth={2}
              dot={false}
              connectNulls
            />
            <Line
              type="monotone"
              dataKey="spy"
              name="SPY (benchmark)"
              stroke="hsl(var(--muted-foreground))"
              strokeWidth={1.5}
              strokeDasharray="4 2"
              dot={false}
              connectNulls
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

// ── CSV export ────────────────────────────────────────────────────

function exportCSV(rows: Record<string, unknown>[], filename: string) {
  if (!rows.length) return;
  const headers = Object.keys(rows[0]);
  const csv = [
    headers.join(","),
    ...rows.map((r) =>
      headers
        .map((h) => {
          const v = r[h];
          const s = String(v ?? "");
          return s.includes(",") || s.includes('"') ? `"${s.replace(/"/g, '""')}"` : s;
        })
        .join(",")
    ),
  ].join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Transaction history ───────────────────────────────────────────

interface TransactionRow {
  id: string;
  symbol: string;
  market: string;
  tx_type: string;
  quantity: number;
  price: number;
  fx_rate: number;
  tx_date: string;
  notes: string | null;
  created_at: string;
}

function TransactionHistory({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<TransactionRow | null>(null);
  const deleteTx = useDeleteTransaction(portfolioId);
  const { data: txns = [], isLoading } = useQuery<TransactionRow[]>({
    queryKey: ["portfolio-transactions", portfolioId],
    queryFn: () =>
      api.get(`/portfolio/${portfolioId}/transactions?limit=200`).then((r) => r.data),
    staleTime: 30_000,
  });

  function handleExport() {
    exportCSV(
      txns.map((tx) => ({
        date: tx.tx_date,
        symbol: tx.symbol,
        market: tx.market,
        type: tx.tx_type,
        quantity: tx.quantity,
        price: tx.price,
        fx_rate: tx.fx_rate,
        value: tx.quantity * tx.price,
        notes: tx.notes ?? "",
      })),
      `transactions-${portfolioId}.csv`
    );
  }

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-foreground font-medium">{t("portfolio.transactions.title")}</h2>
        <button
          onClick={handleExport}
          disabled={!txns.length}
          className="text-xs text-primary hover:underline disabled:opacity-40"
        >
          CSV
        </button>
      </div>

      {isLoading && <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>}
      {!isLoading && txns.length === 0 && (
        <p className="text-xs text-muted-foreground py-4 text-center">{t("portfolio.transactions.no_transactions")}</p>
      )}

      {txns.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="text-left py-2 pr-4 font-medium">{t("portfolio.transactions.executed_at")}</th>
                <th className="text-left py-2 px-2 font-medium">{t("portfolio.holdings.symbol")}</th>
                <th className="text-left py-2 px-2 font-medium">{t("portfolio.holdings.market")}</th>
                <th className="text-left py-2 px-2 font-medium">{t("portfolio.transactions.type")}</th>
                <th className="text-right py-2 px-2 font-medium">{t("portfolio.transactions.qty")}</th>
                <th className="text-right py-2 px-2 font-medium">{t("portfolio.transactions.price")}</th>
                <th className="text-right py-2 px-2 font-medium">{t("portfolio.transactions.fx_rate")}</th>
                <th className="text-right py-2 px-2 font-medium">{t("portfolio.holdings.value")}</th>
                <th className="text-left py-2 pl-4 font-medium">Notes</th>
                <th className="px-2"></th>
              </tr>
            </thead>
            <tbody>
              {txns.map((tx) => (
                <tr key={tx.id} className="border-b border-border/30 hover:bg-accent/5 group">
                  <td className="py-1.5 pr-4 text-muted-foreground">{tx.tx_date}</td>
                  <td className="py-1.5 px-2 font-medium text-primary">{tx.symbol}</td>
                  <td className="py-1.5 px-2 text-muted-foreground">{tx.market}</td>
                  <td className={`py-1.5 px-2 font-medium capitalize ${
                    tx.tx_type === "buy"      ? "text-green-400"
                    : tx.tx_type === "sell"   ? "text-red-400"
                    : "text-amber-400"
                  }`}>{tx.tx_type}</td>
                  <td className="py-1.5 px-2 text-right text-foreground">
                    {tx.quantity.toLocaleString()}
                  </td>
                  <td className="py-1.5 px-2 text-right text-foreground">
                    {tx.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </td>
                  <td className="py-1.5 px-2 text-right text-muted-foreground">
                    {tx.fx_rate === 1 ? "—" : tx.fx_rate.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 })}
                  </td>
                  <td className="py-1.5 px-2 text-right text-muted-foreground whitespace-nowrap">
                    {(tx.quantity * tx.price).toLocaleString(undefined, { maximumFractionDigits: 0 })}
                    <span className="ml-1 text-[10px] opacity-60">{tx.market === "TW" ? "TWD" : "USD"}</span>
                  </td>
                  <td className="py-1.5 pl-4 text-muted-foreground max-w-[160px] truncate">
                    {tx.notes ?? ""}
                  </td>
                  <td className="py-1.5 px-2 text-right whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={() => setEditing(tx)}
                      className="text-muted-foreground hover:text-foreground mr-2"
                      title={t("common.edit")}
                    >✎</button>
                    <button
                      onClick={() => {
                        if (confirm(t("portfolio.confirm_delete_tx"))) deleteTx.mutate(tx.id);
                      }}
                      className="text-muted-foreground hover:text-red-400"
                      title={t("common.delete")}
                    >×</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <EditTransactionModal
          portfolioId={portfolioId}
          tx={editing}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────
export default function PortfolioPage() {
  const { t } = useTranslation();
  const { data: portfolios, isLoading } = usePortfolios();
  const [selected, setSelected] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showAddTx, setShowAddTx] = useState(false);
  const [showEdit, setShowEdit] = useState(false);


  const { data: detail, isFetching } = usePortfolioDetail(selected);
  const deleteP = useDeletePortfolio();
  const optimise = useOptimise(selected ?? "");

  const navigate = useNavigate();
  const activeId = selected ?? portfolios?.[0]?.id ?? null;

  function analyseWithAI() {
    navigate("/ai", {
      state: {
        agentId: "portfolio_advisor",
        initialMessage: "Review my portfolio and suggest improvements to the allocation, risk profile, and any concentration risks.",
        context: { portfolio: detail ?? null },
      },
    });
  }

  return (
    <div className="min-h-screen bg-background p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-primary">{t("portfolio.title")}</h1>
        <div className="flex gap-2">
          {detail && (
            <button
              onClick={analyseWithAI}
              className="px-4 py-2 text-sm bg-primary/10 border border-primary/30 text-primary rounded-md hover:bg-primary/20 transition-colors"
            >
              🤖 {t("nav.ai")}
            </button>
          )}
          <button onClick={() => setShowCreate(true)} className="px-4 py-2 text-sm bg-primary text-primary-foreground rounded-md hover:opacity-90">
            + {t("portfolio.new_portfolio")}
          </button>
        </div>
      </div>

      {isLoading && <p className="text-muted-foreground text-sm">{t("common.loading")}</p>}

      {/* Portfolio selector tabs */}
      {portfolios && portfolios.length > 0 && (
        <div className="flex gap-2 flex-wrap">
          {portfolios.map((p) => (
            <button
              key={p.id}
              onClick={() => setSelected(p.id)}
              className={`px-4 py-1.5 text-sm rounded-full border transition-colors ${
                (selected ?? portfolios[0].id) === p.id
                  ? "bg-primary text-primary-foreground border-primary"
                  : "border-border text-muted-foreground hover:text-foreground"
              }`}
            >
              {p.name} <span className="text-xs opacity-60">{p.currency}</span>
            </button>
          ))}
        </div>
      )}

      {/* Portfolio detail */}
      {activeId && (
        <PortfolioDetail
          portfolioId={activeId}
          detail={detail}
          isFetching={isFetching}
          onAddTx={() => setShowAddTx(true)}
          onEdit={() => setShowEdit(true)}
          onDelete={async () => {
            await deleteP.mutateAsync(activeId);
            setSelected(null);
          }}
          optimiseResult={optimise.data}
          optimisePending={optimise.isPending}
          onRunOptimise={(risk: string) => optimise.mutate({ target_risk: risk, max_weight: 1 })}
        />
      )}

      {!portfolios?.length && !isLoading && (
        <div className="text-center py-16 text-muted-foreground">
          <p className="text-lg">{t("portfolio.no_portfolios")}</p>
        </div>
      )}

      {showCreate && <CreatePortfolioModal onClose={() => setShowCreate(false)} />}
      {showAddTx && activeId && <AddTransactionForm portfolioId={activeId} onClose={() => setShowAddTx(false)} />}
      {showEdit && activeId && detail && (
        <EditPortfolioModal
          portfolioId={activeId}
          currentName={detail.name}
          currentCurrency={detail.currency}
          onClose={() => setShowEdit(false)}
        />
      )}
    </div>
  );
}

// ── Portfolio detail panel ────────────────────────────────────────
function PortfolioDetail({
  portfolioId, detail, isFetching,
  onAddTx, onEdit, onDelete,
  optimiseResult, optimisePending, onRunOptimise,
}: any) {
  const { t } = useTranslation();
  const [detailTab, setDetailTab] = useState<"overview" | "transactions">("overview");

  if (!detail) return <div className="text-muted-foreground text-sm">{isFetching ? t("common.loading") : ""}</div>;

  const pnlPositive = detail.total_pnl >= 0;

  function exportHoldings() {
    exportCSV(
      detail.holdings.map((h: any) => ({
        symbol: h.symbol,
        market: h.market,
        quantity: h.quantity,
        avg_cost: h.avg_cost,
        current_price: h.current_price,
        current_value: h.current_value,
        unrealized_pnl: h.unrealized_pnl,
        unrealized_pnl_pct: h.unrealized_pnl_pct,
        weight_pct: h.weight_pct,
      })),
      `holdings-${portfolioId}.csv`
    );
  }

  return (
    <div className="space-y-6">
      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { label: t("portfolio.summary.total_value"), value: `${detail.currency} ${detail.total_value.toLocaleString()}` },
          { label: t("portfolio.summary.total_cost"),  value: `${detail.currency} ${detail.total_cost.toLocaleString()}` },
          {
            label: t("portfolio.summary.unrealized_pnl"),
            value: `${pnlPositive ? "+" : ""}${detail.total_pnl.toLocaleString(undefined, { maximumFractionDigits: 0 })}`,
            color: pnlPositive ? "text-positive" : "text-negative",
          },
          {
            label: t("portfolio.summary.performance"),
            value: `${pnlPositive ? "+" : ""}${detail.total_pnl_pct.toFixed(2)}%`,
            color: pnlPositive ? "text-positive" : "text-negative",
          },
        ].map((c) => (
          <div key={c.label} className="bg-card border border-border rounded-lg p-4">
            <p className="text-xs text-muted-foreground">{c.label}</p>
            <p className={`text-lg font-semibold mt-1 ${c.color ?? "text-foreground"}`}>{c.value}</p>
          </div>
        ))}
      </div>

      {/* Tab selector */}
      <div className="flex gap-1 bg-secondary/30 p-1 rounded-lg w-fit">
        {(["overview", "transactions"] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setDetailTab(tab)}
            className={`px-4 py-1.5 text-sm rounded-md transition-colors capitalize ${
              detailTab === tab
                ? "bg-card text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab === "overview" ? t("portfolio.tabs.holdings") : t("portfolio.tabs.transactions")}
          </button>
        ))}
      </div>

      {detailTab === "overview" && (
        <>
          {/* Holdings + pie */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-2 bg-card border border-border rounded-lg p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-foreground font-medium">{t("portfolio.tabs.holdings")}</h2>
                <div className="flex gap-2">
                  <button
                    onClick={exportHoldings}
                    disabled={!detail.holdings.length}
                    className="text-xs text-primary hover:underline disabled:opacity-40"
                  >
                    CSV
                  </button>
                  <button onClick={onAddTx} className="text-xs px-3 py-1 bg-primary/10 text-primary rounded hover:bg-primary/20">
                    + {t("portfolio.transactions.add")}
                  </button>
                </div>
              </div>
              <HoldingsTable holdings={detail.holdings} currency={detail.currency} />
            </div>

            <div className="bg-card border border-border rounded-lg p-5">
              <h2 className="text-foreground font-medium mb-2">{t("portfolio.tabs.allocation")}</h2>
              <AllocationPie holdings={detail.holdings} />
            </div>
          </div>

          {/* Performance chart */}
          <PerformanceChart portfolioId={portfolioId} />

      {/* Expert evaluation — pick a persona, get an in-place AI review */}
      <ExpertEvaluationCard portfolioId={portfolioId} detail={detail} />

      {/* Optimiser */}
      <div className="bg-card border border-border rounded-lg p-5 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-foreground font-medium">{t("portfolio.optimizer.title")}</h2>
            <p className="text-xs text-muted-foreground mt-0.5">{t("portfolio.optimizer.description")}</p>
          </div>
          <div className="flex gap-2">
            {([
              { key: "low", label: t("portfolio.optimizer.risk_low") },
              { key: "medium", label: t("portfolio.optimizer.risk_medium") },
              { key: "high", label: t("portfolio.optimizer.risk_high") },
            ] as const).map(({ key, label }) => (
              <button
                key={key}
                onClick={() => onRunOptimise(key)}
                disabled={optimisePending}
                className="px-3 py-1 text-xs border border-border rounded hover:bg-secondary/50 disabled:opacity-40"
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {optimisePending && <p className="text-sm text-muted-foreground animate-pulse">{t("portfolio.optimizer.running")}</p>}

        {optimiseResult && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {[
                { k: t("portfolio.optimizer.expected_return"), v: `${(optimiseResult.metrics.expected_annual_return * 100).toFixed(1)}%` },
                { k: t("portfolio.optimizer.expected_vol"),    v: `${(optimiseResult.metrics.annual_volatility * 100).toFixed(1)}%` },
                { k: t("portfolio.optimizer.sharpe"),          v: optimiseResult.metrics.sharpe_ratio.toFixed(2) },
                { k: t("analytics.backtest.max_drawdown"),     v: `${(optimiseResult.metrics.max_drawdown * 100).toFixed(1)}%` },
              ].map((m) => (
                <div key={m.k} className="bg-secondary/30 rounded p-3">
                  <p className="text-xs text-muted-foreground">{m.k}</p>
                  <p className="text-sm font-medium text-foreground mt-0.5">{m.v}</p>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap gap-2">
              {Object.entries(optimiseResult.weights).map(([sym, w]: [string, any]) => (
                <span key={sym} className="text-xs bg-secondary/40 px-2 py-1 rounded">
                  {sym} <span className="text-primary font-medium">{(w * 100).toFixed(1)}%</span>
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="flex justify-end gap-3">
        <button
          onClick={onEdit}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          {t("common.edit")}
        </button>
        <button
          onClick={() => {
            if (confirm(t("portfolio.confirm_delete_portfolio"))) onDelete();
          }}
          className="text-xs text-negative/70 hover:text-negative"
        >
          {t("common.delete")}
        </button>
      </div>
        </>
      )}

      {detailTab === "transactions" && (
        <TransactionHistory portfolioId={portfolioId} />
      )}
    </div>
  );
}
