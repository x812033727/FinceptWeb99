/**
 * NLQueryBar — natural-language screening input (功能 B2).
 *
 * The user types an intent like「近月外資買超且本益比低於 20 的電子股」;
 * we call /api/ai/chat with a system nudge (via `context`) so the model
 * turns it into ONE `run_screener` tool call. The tool_result frame
 * carries the full rows JSON (the backend raises the SSE summary cap
 * for run_screener); we parse it, map rows to the ScreenerResult shape
 * and hand them to the page, which renders them in the existing
 * ResultsTable. The assistant's streamed text becomes the one-line
 * summary shown above the table.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { notifyRateLimited } from "@/lib/api";
import { useSessionAbortController } from "@/hooks/useSessionAbortController";
import { useAuthStore } from "@/store/authStore";
import type { Market, ScreenerResult } from "@/types/market";

export interface NLScreenerResult {
  rows: ScreenerResult[];
  market: Market;
}

// Nudge prepended to the persona's system prompt as a <context> JSON
// block by the chat endpoint — steers the model to a single
// run_screener call and a one-line zh-TW summary.
const NL_SCREENER_NUDGE = {
  nl_screener_instruction:
    "使用者正在選股器頁面輸入自然語言選股條件。請將其意圖轉換為一次 " +
    "run_screener 工具呼叫(選擇正確的 market 與 filters;台股 market='TW'," +
    "美股 market='US')。工具回傳後,以一句繁體中文總結篩選條件與結果數量即可 " +
    "— 不要輸出表格或逐檔清單,結果由前端表格渲染。",
};

interface RawScreenerRow {
  symbol?: string;
  market?: string;
  name?: string;
  name_zh?: string;
  price?: number | null;
  change_pct?: number | null;
  volume?: number | null;
  market_cap?: number | null;
  pe_ratio?: number | null;
  pb_ratio?: number | null;
  dividend_yield?: number | null;
  sector?: string | null;
  exchange?: string | null;
  data_source?: string | null;
}

function mapRows(payload: { market?: string; rows?: RawScreenerRow[] }): NLScreenerResult | null {
  if (!payload || !Array.isArray(payload.rows)) return null;
  const market: Market = payload.market === "TW" ? "TW" : "US";
  const rows: ScreenerResult[] = payload.rows
    .filter((r) => r && typeof r.symbol === "string")
    .map((r) => ({
      symbol: r.symbol as string,
      market,
      name: r.name ?? r.name_zh ?? "",
      price: r.price ?? 0,
      change_pct: r.change_pct ?? 0,
      volume: r.volume ?? 0,
      market_cap: r.market_cap ?? undefined,
      pe_ratio: r.pe_ratio ?? undefined,
      pb_ratio: r.pb_ratio ?? undefined,
      dividend_yield: r.dividend_yield ?? undefined,
      sector: r.sector ?? undefined,
      exchange: r.exchange ?? undefined,
      data_source: r.data_source ?? undefined,
    }));
  return { rows, market };
}

export function NLQueryBar({
  onResult,
}: {
  /** Called with mapped rows on success, or null when results are cleared. */
  onResult: (result: NLScreenerResult | null) => void;
}) {
  const { t } = useTranslation();
  const token = useAuthStore((s) => s.token);
  const { renew } = useSessionAbortController();
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [summary, setSummary] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hasResult, setHasResult] = useState(false);

  function clearResult() {
    setSummary(null);
    setError(null);
    setHasResult(false);
    onResult(null);
  }

  async function submit() {
    const text = query.trim();
    if (!text || loading) return;
    setLoading(true);
    setError(null);
    setSummary(null);

    let assembled = "";
    let mapped: NLScreenerResult | null = null;
    let streamError: string | null = null;
    const ctrl = renew();

    try {
      const resp = await fetch("/api/ai/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          // claude_research is the tool-capable persona (default provider
          // claude_agent; admins may re-point it at any tool-capable
          // OpenAI-compat provider via PersonaOverride).
          agent_id: "claude_research",
          messages: [{ role: "user", content: text }],
          context: NL_SCREENER_NUDGE,
        }),
        signal: ctrl.signal,
      });

      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}) as { detail?: string });
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
        const frames = buf.split("\n\n");
        buf = frames.pop() ?? "";
        for (const frame of frames) {
          if (!frame.startsWith("data: ")) continue;
          const payload = frame.slice(6).trim();
          if (payload === "[DONE]") break;
          try {
            const obj = JSON.parse(payload);
            if (obj.error) { streamError = obj.error; break; }
            if (obj.delta) assembled += obj.delta;
            if (
              obj.tool_result &&
              typeof obj.tool_result.name === "string" &&
              obj.tool_result.name.endsWith("run_screener") &&
              !obj.tool_result.is_error
            ) {
              try {
                const parsed = mapRows(JSON.parse(obj.tool_result.summary));
                if (parsed) mapped = parsed;
              } catch { /* truncated / non-JSON summary — ignore */ }
            }
          } catch { /* malformed frame — ignore */ }
        }
        if (streamError) break;
      }
    } catch (e: unknown) {
      streamError = (e as Error).message;
    } finally {
      setLoading(false);
    }

    if (mapped) {
      setSummary(assembled.trim() || null);
      setHasResult(true);
      onResult(mapped);
      if (streamError) setError(streamError);
    } else {
      setError(streamError ?? t("screener.nl.no_result"));
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-2">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void submit(); }}
          placeholder={t("screener.nl.placeholder")}
          disabled={loading}
          aria-label={t("screener.nl.placeholder")}
          className="flex-1 bg-background border border-border rounded px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:border-primary/50"
        />
        <button
          onClick={() => void submit()}
          disabled={loading || !query.trim()}
          className="px-4 py-2 text-sm font-medium rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
        >
          {loading ? t("screener.nl.loading") : t("screener.nl.submit")}
        </button>
        {hasResult && !loading && (
          <button
            onClick={clearResult}
            className="px-3 py-2 text-sm rounded border border-border text-muted-foreground hover:text-foreground hover:bg-accent/10 shrink-0"
          >
            {t("screener.nl.clear")}
          </button>
        )}
      </div>

      {summary && (
        <p className="text-xs text-muted-foreground bg-accent/5 border border-border rounded px-3 py-2">
          {summary}
        </p>
      )}
      {error && (
        <p className="text-xs text-danger bg-danger/10 border border-danger/30 rounded px-3 py-2">
          {error}
        </p>
      )}
    </div>
  );
}
