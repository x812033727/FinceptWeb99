import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import api, { notifyRateLimited } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";
import type { AgentInfo } from "@/types/discussion";

// In-place AI evaluation of the active portfolio. Lets the user pick
// any of the 19 personas (Buffett / Lynch / Dalio / quant / functional
// CFA …) and streams the response inline rather than navigating to
// /ai. Reuses the same /api/ai/chat SSE endpoint as the AI page;
// tool calls show inline so claude_research's research workflow
// stays visible.
//
// Quota: each evaluation = 1 AI request, identical to a normal chat
// turn.

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

export function ExpertEvaluationCard({ detail }: { portfolioId: string; detail: any }) {
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
            className="px-4 py-1.5 rounded bg-danger/10 border border-danger/40 text-danger text-sm hover:bg-danger/20"
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
                      tc.status === "running" ? "bg-warning animate-pulse"
                      : tc.status === "error" ? "bg-danger"
                      : "bg-success"
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
            <p className="text-xs text-danger bg-danger/10 border border-danger/30 rounded px-3 py-2">{error}</p>
          )}

          <p className="text-[10px] text-muted-foreground">
            {t("ai.disclaimer")} · {t("portfolio.expert_eval.uses_quota", { count: 1 })}
          </p>
        </div>
      )}
    </div>
  );
}
