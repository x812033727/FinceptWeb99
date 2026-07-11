/**
 * B1 個股 AI 研究報告 panel — the "AI 報告" tab on StockDetailPage.
 *
 * Generate button streams `POST /api/ai/stock-report/{market}/{symbol}`
 * (SSE, same `data: {"delta": ...}` frame contract as AIPage's chat
 * stream — the reader below is modelled on `_useChatStream`) into a
 * markdown-rendered pane, then the backend archives the finished
 * report; the collapsible history list reads it back via
 * `GET /api/ai/stock-reports`.
 *
 * Error states mirror AIPage's handling: a 429 surfaces the quota
 * toast (`notifyRateLimited`) plus an inline quota message; a
 * missing-provider-key error (the backend converts the provider's
 * "[X API key not configured]" placeholder into an SSE error frame)
 * shows a dedicated hint instead of a raw error string.
 */
import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { FileText, Loader2, Sparkles, Square } from "lucide-react";
import api, { notifyRateLimited } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";
import { CollapsibleHeader } from "@/components/Collapsible";
import { renderInlineMarkdown } from "@/components/discussion/_format";
import { formatTaipei } from "@/lib/timeFormat";

// ── types ──────────────────────────────────────────────────────────

interface ReportSummary {
  id: string;
  symbol: string;
  market: string;
  model: string;
  created_at: string;
  preview: string;
}

interface ReportDetail extends ReportSummary {
  content_md: string;
}

type ErrorKind = "quota" | "no_key" | "generic";

// ── markdown pane ──────────────────────────────────────────────────
//
// The report is line-oriented markdown (## headings, - bullets,
// **bold** + signed percents). Reuse the discussion subsystem's
// inline renderer for the bold/percent colouring and add the
// block-level treatment locally — same approach TurnBubble takes.

function renderReportMarkdown(content: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  content.split("\n").forEach((line, i) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const heading = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (heading) {
      out.push(
        <h3
          key={`h-${i}`}
          className="text-sm font-semibold text-foreground mt-4 mb-1.5 first:mt-0"
        >
          {renderInlineMarkdown(heading[2])}
        </h3>,
      );
      return;
    }
    const bullet = /^[-*]\s+(.*)$/.exec(trimmed);
    if (bullet) {
      out.push(
        <div key={`b-${i}`} className="flex gap-2 text-sm leading-7">
          <span className="text-muted-foreground shrink-0">•</span>
          <span>{renderInlineMarkdown(bullet[1])}</span>
        </div>,
      );
      return;
    }
    out.push(
      <p key={`p-${i}`} className="text-sm leading-7">
        {renderInlineMarkdown(trimmed)}
      </p>,
    );
  });
  return out;
}

// ── api helpers ────────────────────────────────────────────────────

async function fetchReports(market: string, symbol: string): Promise<ReportSummary[]> {
  const res = await api.get<ReportSummary[]>(
    `/ai/stock-reports?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(market)}`,
  );
  return res.data;
}

async function fetchReport(id: string): Promise<ReportDetail> {
  const res = await api.get<ReportDetail>(`/ai/stock-reports/${id}`);
  return res.data;
}

function classifyError(message: string): ErrorKind {
  if (/quota/i.test(message) || message.includes("配額") || message.includes("額度")) {
    return "quota";
  }
  if (/api key/i.test(message) || message.includes("金鑰")) {
    return "no_key";
  }
  return "generic";
}

// ── panel ──────────────────────────────────────────────────────────

export function StockAIReportPanel({ symbol, market }: { symbol: string; market: string }) {
  const { t } = useTranslation();
  const token = useAuthStore((s) => s.token);
  const queryClient = useQueryClient();

  const [content, setContent] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [stage, setStage] = useState<"context" | "generating" | null>(null);
  const [error, setError] = useState<{ kind: ErrorKind; message: string } | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  // Which saved report is displayed; null = live/most-recent stream.
  const [viewingId, setViewingId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const { data: reports = [] } = useQuery({
    queryKey: ["stock-reports", market, symbol],
    queryFn: () => fetchReports(market, symbol),
    staleTime: 30_000,
  });

  async function generate() {
    if (streaming) return;
    setError(null);
    setContent("");
    setViewingId(null);
    setStreaming(true);
    setStage("context");

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let assembled = "";

    try {
      const resp = await fetch(`/api/ai/stock-report/${market}/${symbol}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({}),
        signal: ctrl.signal,
      });

      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        const detail: string = data.detail ?? `HTTP ${resp.status}`;
        if (resp.status === 429) {
          const retryAfter = Number(resp.headers.get("retry-after")) || undefined;
          notifyRateLimited(detail, retryAfter);
          setError({ kind: "quota", message: detail });
          return;
        }
        setError({ kind: classifyError(detail), message: detail });
        return;
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
            if (obj.stage === "context" || obj.stage === "generating") {
              setStage(obj.stage);
            }
            if (obj.error) {
              setError({ kind: classifyError(obj.error), message: obj.error });
              break;
            }
            if (obj.delta) {
              assembled += obj.delta;
              setContent(assembled);
            }
            if (obj.done) {
              // Report archived server-side — refresh the history list.
              void queryClient.invalidateQueries({
                queryKey: ["stock-reports", market, symbol],
              });
            }
          } catch {
            /* ignore malformed frame */
          }
        }
      }
    } catch (e: unknown) {
      if ((e as Error).name !== "AbortError") {
        setError({ kind: classifyError((e as Error).message), message: (e as Error).message });
      }
    } finally {
      setStreaming(false);
      setStage(null);
    }
  }

  function stopGeneration() {
    abortRef.current?.abort();
  }

  async function openReport(id: string) {
    if (streaming) return;
    setError(null);
    try {
      const detail = await fetchReport(id);
      setViewingId(id);
      setContent(detail.content_md);
    } catch {
      setError({ kind: "generic", message: t("stock.ai_report.load_failed") });
    }
  }

  const showEmpty = !content && !streaming && !error;

  return (
    <div className="p-4 space-y-4">
      {/* header row: title + generate/stop button */}
      <div className="flex items-center gap-2 flex-wrap">
        <h3 className="text-sm font-semibold text-foreground flex items-center gap-1.5">
          <Sparkles size={14} className="text-primary" aria-hidden="true" />
          {t("stock.ai_report.title")}
        </h3>
        <div className="ml-auto flex items-center gap-2">
          {streaming ? (
            <button
              type="button"
              onClick={stopGeneration}
              className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded border border-border text-muted-foreground hover:text-foreground transition-colors"
            >
              <Square size={12} aria-hidden="true" />
              {t("stock.ai_report.stop")}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void generate()}
              className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded bg-primary text-primary-foreground hover:opacity-90 transition-opacity"
            >
              <Sparkles size={12} aria-hidden="true" />
              {content ? t("stock.ai_report.regenerate") : t("stock.ai_report.generate")}
            </button>
          )}
        </div>
      </div>

      {/* streaming progress line */}
      {streaming && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 size={13} className="animate-spin" aria-hidden="true" />
          {stage === "context"
            ? t("stock.ai_report.stage_context")
            : t("stock.ai_report.stage_generating")}
        </div>
      )}

      {/* error states — quota / missing key get dedicated copy */}
      {error && (
        <div
          className={
            error.kind === "generic"
              ? "text-xs text-danger bg-danger/10 border border-danger/30 rounded px-3 py-2"
              : "text-xs text-warning bg-warning/10 border border-warning/30 rounded px-3 py-2"
          }
        >
          {error.kind === "quota"
            ? t("stock.ai_report.quota_exceeded")
            : error.kind === "no_key"
              ? t("stock.ai_report.no_key")
              : error.message}
        </div>
      )}

      {/* report pane */}
      {showEmpty ? (
        <p className="text-sm text-muted-foreground py-8 text-center">
          {t("stock.ai_report.empty")}
        </p>
      ) : content ? (
        <div className="max-w-prose space-y-1.5" data-testid="ai-report-content">
          {viewingId && (
            <p className="text-[10px] text-muted-foreground">
              {t("stock.ai_report.viewing_saved", {
                when: formatTaipei(
                  reports.find((r) => r.id === viewingId)?.created_at ?? "",
                  "datetime",
                ),
              })}
            </p>
          )}
          {renderReportMarkdown(content)}
          {streaming && (
            <span
              className="inline-block w-1.5 h-3.5 bg-current ml-0.5 animate-pulse align-middle"
              aria-hidden="true"
            />
          )}
        </div>
      ) : null}

      {!streaming && content && (
        <p className="text-[10px] text-muted-foreground border-t border-border pt-2">
          {t("stock.ai_report.disclaimer")}
        </p>
      )}

      {/* past reports (collapsible) */}
      <div className="border border-border rounded-lg p-3">
        <CollapsibleHeader
          open={historyOpen}
          toggle={() => setHistoryOpen((o) => !o)}
          title={t("stock.ai_report.past_reports")}
          subtitle={t("stock.ai_report.past_reports_count", { count: reports.length })}
        />
        {historyOpen && (
          <div className="mt-2 space-y-1">
            {reports.length === 0 ? (
              <p className="text-xs text-muted-foreground px-5">
                {t("stock.ai_report.no_past_reports")}
              </p>
            ) : (
              reports.map((r) => (
                <button
                  key={r.id}
                  type="button"
                  onClick={() => void openReport(r.id)}
                  className={`w-full flex items-start gap-2 text-left px-2 py-1.5 rounded hover:bg-accent/20 transition-colors ${
                    viewingId === r.id ? "bg-accent/20" : ""
                  }`}
                >
                  <FileText size={13} className="text-muted-foreground shrink-0 mt-0.5" aria-hidden="true" />
                  <span className="min-w-0 flex-1">
                    <span className="block text-xs text-foreground">
                      {formatTaipei(r.created_at, "datetime")}
                      {r.model ? (
                        <span className="text-muted-foreground"> · {r.model}</span>
                      ) : null}
                    </span>
                    <span className="block text-[11px] text-muted-foreground truncate">
                      {r.preview}
                    </span>
                  </span>
                </button>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  );
}
