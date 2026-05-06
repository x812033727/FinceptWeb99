import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import type { Conclusion, DiscussionDetail, Turn } from "@/types/discussion";
import { useCollapsible } from "@/hooks/useCollapsible";
import { ConclusionHero } from "./ConclusionHero";

function buildMarkdownExport(
  detail: DiscussionDetail,
  personaName: (id: string) => string,
): string {
  const lines: string[] = [];
  lines.push(`# ${detail.topic}`);
  lines.push("");
  lines.push("## 共同規則");
  lines.push("");
  lines.push("```");
  lines.push(detail.rules);
  lines.push("```");
  lines.push("");
  lines.push("## 出席專家");
  lines.push("");
  for (const pid of detail.persona_ids) {
    lines.push(`- ${personaName(pid)} (${pid})`);
  }
  lines.push("");

  // Group turns by round so the markdown reads chronologically.
  const turnsByRound = new Map<number, Turn[]>();
  for (const tn of detail.turns) {
    if (!turnsByRound.has(tn.round)) turnsByRound.set(tn.round, []);
    turnsByRound.get(tn.round)!.push(tn);
  }
  const sortedRounds = [...turnsByRound.keys()].sort((a, b) => a - b);
  for (const r of sortedRounds) {
    lines.push(`## 第 ${r} 輪`);
    lines.push("");
    const roundTurns = (turnsByRound.get(r) ?? []).slice().sort(
      (a, b) => a.turn_index - b.turn_index,
    );
    for (const tn of roundTurns) {
      const stanceLabel =
        tn.stance === "agree" ? "✓ 同意" :
        tn.stance === "dissent" ? "✗ 異議" : "↳ 補充";
      lines.push(`### ${personaName(tn.persona_id)} — ${stanceLabel}`);
      lines.push("");
      lines.push(tn.content.trim() || "_（同意，無補充）_");
      lines.push("");
    }
  }

  if (detail.conclusion) {
    const c = detail.conclusion;
    lines.push("## 結論");
    lines.push("");
    if (c.recommended_symbols.length) {
      lines.push(`- 推薦標的：${c.recommended_symbols.join(", ")}`);
    }
    lines.push(`- 共識度：${(c.consensus_score * 100).toFixed(0)}%`);
    lines.push(`- 時間框架：${c.time_horizon}`);
    lines.push("");
    lines.push("### 理由");
    lines.push("");
    lines.push(c.reasoning);
    if (c.risks.length) {
      lines.push("");
      lines.push("### 風險");
      lines.push("");
      for (const risk of c.risks) {
        lines.push(`- ${risk}`);
      }
    }
  }
  lines.push("");
  return lines.join("\n");
}

/**
 * Reusable conclusion card. Defaults to rendering `detail.conclusion`
 * (the original synthesizer output) but accepts an explicit
 * `conclusion` override so the same component can render
 * `detail.post_mortem_conclusion` (PR #272 — preserved separately
 * so the original-vs-post-mortem comparison stays visible). The
 * `variant` switch tints the card and chooses the title /
 * collapse-storage key so the two cards don't collide.
 */
export function ConclusionCard({
  detail,
  personaName,
  conclusion: overrideConclusion,
  variant = "primary",
}: {
  detail: DiscussionDetail;
  personaName: (id: string) => string;
  conclusion?: Conclusion;
  variant?: "primary" | "post_mortem";
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const conclusion = overrideConclusion ?? detail.conclusion!;

  // Triggers a browser download of the rendered markdown. Filename uses
  // the topic (sanitised) + ISO date so users can recognise exports
  // among many.
  function exportMarkdown() {
    const md = buildMarkdownExport(detail, personaName);
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const safeTopic = detail.topic.slice(0, 40).replace(/[\\/:*?"<>|]/g, "_");
    const date = new Date().toISOString().slice(0, 10);
    const a = document.createElement("a");
    a.href = url;
    a.download = `discussion-${date}-${safeTopic}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // Hands off the conclusion to the AIPage for deeper one-on-one analysis.
  function askPersonaAbout(symbol?: string) {
    const seedPersona = detail.persona_ids[0];
    const target = symbol ?? conclusion.recommended_symbols[0] ?? "";
    const prompt = symbol
      ? t("discussion.followup_prompt_symbol", { symbol })
      : t("discussion.followup_prompt_general");
    navigate("/ai", {
      state: {
        agentId: seedPersona,
        initialMessage: prompt,
        context: {
          source: "discussion",
          topic: detail.topic,
          recommended_symbols: conclusion.recommended_symbols,
          consensus_score: conclusion.consensus_score,
          time_horizon: conclusion.time_horizon,
          focused_symbol: target || undefined,
        },
      },
    });
  }

  // When the synthesizer's output couldn't be parsed as JSON we treat
  // the conclusion as degraded — paint the card red, hide the actions
  // (export / deep-dive) so users don't accidentally rely on garbage,
  // and prompt them to re-synthesize.
  const hasError = !!conclusion._parse_error;
  // Variant-specific tint so post-mortem cards don't visually collide
  // with the original. Purple matches the post-mortem badge from
  // PR #268 (RoundSection 「📋 事後檢討」 marker) so the two surfaces
  // share a colour language.
  const isPostMortem = variant === "post_mortem";
  // PR-C: gradient background gives the conclusion section a "hero"
  // visual weight distinct from the per-turn bubbles above it. Two
  // semantic stops: dark tinted top → near-black bottom, with a
  // matching tinted border.
  const baseClass = isPostMortem
    ? "bg-gradient-to-br from-purple-950/40 to-purple-950/10 border border-purple-800/50 rounded-lg p-4 mt-6 shadow-sm"
    : "bg-gradient-to-br from-amber-950/40 to-amber-950/10 border border-amber-800/50 rounded-lg p-4 mt-6 shadow-sm";
  const cardClass = hasError
    ? "bg-red-950/20 border border-red-800/60 rounded-lg p-4 mt-6"
    : baseClass;
  const okTitleClass = isPostMortem
    ? "text-sm font-semibold text-purple-300"
    : "text-sm font-semibold text-amber-300";
  const titleClass = hasError ? "text-sm font-semibold text-red-300" : okTitleClass;
  const titleKey = isPostMortem
    ? "discussion.post_mortem_conclusion_title"
    : "discussion.conclusion_title";
  // Conclusion is the ONE block that defaults open per the user's
  // UX rule ("at most only the conclusion is default-expanded"). All
  // other transcript sections collapse on render. Persisted per
  // discussion id (and per variant — `.conclusion` vs
  // `.post_mortem_conclusion`) so the two cards toggle independently
  // and the user's collapse pref for one doesn't fold the other.
  const collapseStorageKey = isPostMortem
    ? `discussion.${detail.id}.post_mortem_conclusion`
    : `discussion.${detail.id}.conclusion`;
  const { open, toggle } = useCollapsible(collapseStorageKey, true);

  return (
    <div className={cardClass}>
      <div className="flex items-center justify-between mb-2 gap-2">
        <button
          type="button"
          onClick={toggle}
          aria-expanded={open}
          className="flex items-center gap-2 text-left hover:opacity-80 transition-opacity flex-1 min-w-0"
        >
          <span className="text-[10px] text-muted-foreground w-3 inline-block shrink-0">
            {open ? "▼" : "▶"}
          </span>
          <h3 className={titleClass}>
            {hasError ? `⚠ ${t(titleKey)}` : t(titleKey)}
          </h3>
        </button>
        {!hasError && (
          <div
            className="flex gap-1.5 shrink-0"
            // Action buttons must NOT toggle the card — stop bubbling.
            onClick={(e) => e.stopPropagation()}
          >
            <button
              onClick={exportMarkdown}
              className="px-2.5 py-1 text-xs border border-border text-muted-foreground rounded hover:text-foreground"
            >
              {t("discussion.export_markdown")}
            </button>
            {conclusion.recommended_symbols.length > 0 && (
              <button
                onClick={() => askPersonaAbout()}
                className="px-2.5 py-1 text-xs border border-amber-800/50 text-amber-300 rounded hover:bg-amber-900/30"
              >
                {t("discussion.ask_followup")}
              </button>
            )}
          </div>
        )}
      </div>
      {open && (hasError ? (
        <p className="text-xs text-red-300">{t("discussion.conclusion_parse_error")}</p>
      ) : (
        <ConclusionHero
          detail={detail}
          conclusion={conclusion}
          variant={variant}
          onSymbolClick={askPersonaAbout}
        />
      ))}
    </div>
  );
}
