/**
 * B5 AI 投組健檢 card — lives on the 風險 tab of PortfolioPage,
 * directly below the C1 risk dashboard.
 *
 * The button streams `POST /api/ai/portfolio-review/{portfolioId}`
 * (SSE, same `data: {"delta": ...}` frame contract as the B1 stock
 * report — the reader and markdown pane below are modelled on
 * `StockAIReportPanel`) into a rendered pane. Reviews are on-demand
 * and NOT persisted, so there is no history list here.
 *
 * Error states mirror StockAIReportPanel: 429 → quota toast + inline
 * quota copy; a missing-provider-key SSE error → dedicated hint; the
 * backend's "no holdings" error → dedicated empty-portfolio copy.
 */
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { HeartPulse, Loader2, Sparkles, Square } from "lucide-react";
import { notifyRateLimited } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";
import { renderInlineMarkdown } from "@/components/discussion/_format";

type ErrorKind = "quota" | "no_key" | "empty_portfolio" | "generic";

function classifyError(message: string): ErrorKind {
  if (/quota/i.test(message) || message.includes("配額") || message.includes("額度")) {
    return "quota";
  }
  if (/api key/i.test(message) || message.includes("金鑰")) {
    return "no_key";
  }
  if (/no holdings/i.test(message) || message.includes("沒有持倉")) {
    return "empty_portfolio";
  }
  return "generic";
}

// Line-oriented markdown → blocks, same approach as the B1 report
// pane (headings / bullets / paragraphs, inline bold + signed percent
// colouring via the discussion subsystem's renderer).
function renderReviewMarkdown(content: string): React.ReactNode[] {
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

export function PortfolioAIReviewCard({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const token = useAuthStore((s) => s.token);

  const [content, setContent] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [stage, setStage] = useState<"context" | "generating" | null>(null);
  const [error, setError] = useState<{ kind: ErrorKind; message: string } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  async function generate() {
    if (streaming) return;
    setError(null);
    setContent("");
    setStreaming(true);
    setStage("context");

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let assembled = "";

    try {
      const resp = await fetch(`/api/ai/portfolio-review/${portfolioId}`, {
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

  const showEmpty = !content && !streaming && !error;

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-4" data-testid="portfolio-ai-review">
      {/* header row: title + generate/stop button */}
      <div className="flex items-center gap-2 flex-wrap">
        <div>
          <h2 className="text-foreground font-medium flex items-center gap-1.5">
            <HeartPulse size={15} className="text-primary" aria-hidden="true" />
            {t("portfolio.ai_review.title")}
          </h2>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t("portfolio.ai_review.subtitle")}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {streaming ? (
            <button
              type="button"
              onClick={stopGeneration}
              className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded border border-border text-muted-foreground hover:text-foreground transition-colors"
            >
              <Square size={12} aria-hidden="true" />
              {t("portfolio.ai_review.stop")}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void generate()}
              className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded bg-primary text-primary-foreground hover:opacity-90 transition-opacity"
            >
              <Sparkles size={12} aria-hidden="true" />
              {content ? t("portfolio.ai_review.regenerate") : t("portfolio.ai_review.generate")}
            </button>
          )}
        </div>
      </div>

      {/* streaming progress line */}
      {streaming && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 size={13} className="animate-spin" aria-hidden="true" />
          {stage === "context"
            ? t("portfolio.ai_review.stage_context")
            : t("portfolio.ai_review.stage_generating")}
        </div>
      )}

      {/* error states — quota / missing key / empty portfolio get dedicated copy */}
      {error && (
        <div
          className={
            error.kind === "generic"
              ? "text-xs text-danger bg-danger/10 border border-danger/30 rounded px-3 py-2"
              : "text-xs text-warning bg-warning/10 border border-warning/30 rounded px-3 py-2"
          }
        >
          {error.kind === "quota"
            ? t("portfolio.ai_review.quota_exceeded")
            : error.kind === "no_key"
              ? t("portfolio.ai_review.no_key")
              : error.kind === "empty_portfolio"
                ? t("portfolio.ai_review.empty_portfolio")
                : error.message}
        </div>
      )}

      {/* review pane */}
      {showEmpty ? (
        <p className="text-sm text-muted-foreground py-6 text-center">
          {t("portfolio.ai_review.empty")}
        </p>
      ) : content ? (
        <div className="max-w-prose space-y-1.5" data-testid="ai-review-content">
          {renderReviewMarkdown(content)}
          {streaming && (
            <span
              className="inline-block w-1.5 h-3.5 bg-current ml-0.5 animate-pulse align-middle"
              aria-hidden="true"
            />
          )}
        </div>
      ) : null}

      {!streaming && content && (
        <p className="text-micro text-muted-foreground border-t border-border pt-2">
          {t("portfolio.ai_review.disclaimer")} · {t("portfolio.ai_review.not_saved")}
        </p>
      )}
    </div>
  );
}

export default PortfolioAIReviewCard;
