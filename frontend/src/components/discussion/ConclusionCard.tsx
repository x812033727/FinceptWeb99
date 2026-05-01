import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import type { DiscussionDetail, Turn } from "@/types/discussion";
import { renderInlineMarkdown } from "./_helpers";

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

export function ConclusionCard({
  detail,
  personaName,
}: {
  detail: DiscussionDetail;
  personaName: (id: string) => string;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const conclusion = detail.conclusion!;

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
  const cardClass = hasError
    ? "bg-red-950/20 border border-red-800/60 rounded-lg p-4 mt-4"
    : "bg-amber-950/20 border border-amber-800/50 rounded-lg p-4 mt-4";
  const titleClass = hasError ? "text-sm font-semibold text-red-300" : "text-sm font-semibold text-amber-300";

  return (
    <div className={cardClass}>
      <div className="flex items-center justify-between mb-2 gap-2">
        <h3 className={titleClass}>
          {hasError
            ? `⚠ ${t("discussion.conclusion_title")}`
            : t("discussion.conclusion_title")}
        </h3>
        {!hasError && (
          <div className="flex gap-1.5 shrink-0">
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
      {hasError ? (
        <p className="text-xs text-red-300">{t("discussion.conclusion_parse_error")}</p>
      ) : (
        <>
          {conclusion.recommended_symbols.length > 0 && (
            <div className="mb-2">
              <span className="text-xs text-muted-foreground">
                {t("discussion.recommended_symbols")}：
              </span>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {conclusion.recommended_symbols.map((s) => (
                  <button
                    key={s}
                    onClick={() => askPersonaAbout(s)}
                    title={t("discussion.click_for_deep_dive")}
                    className="px-2 py-0.5 rounded bg-amber-900/30 text-amber-200 text-xs font-mono hover:bg-amber-900/60"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          <p className="text-sm text-foreground mb-2 whitespace-pre-wrap">
            {renderInlineMarkdown(conclusion.reasoning)}
          </p>
          {conclusion.risks.length > 0 && (
            <div className="mb-2">
              <span className="text-xs text-muted-foreground">
                {t("discussion.risks")}：
              </span>
              <ul className="mt-1 list-disc list-inside text-xs text-foreground/80 space-y-0.5">
                {conclusion.risks.map((r, idx) => (
                  <li key={idx}>{r}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="text-xs text-muted-foreground">
            {t("discussion.consensus")}：
            <span className="font-mono ml-1">
              {(conclusion.consensus_score * 100).toFixed(0)}%
            </span>
            <span className="ml-3">
              {t("discussion.horizon")}：
              <span className="ml-1">
                {t(`discussion.horizon_${conclusion.time_horizon}`)}
              </span>
            </span>
          </div>
        </>
      )}
    </div>
  );
}
