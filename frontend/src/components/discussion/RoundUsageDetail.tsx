import { useTranslation } from "react-i18next";
import { useCollapsible } from "@/hooks/useCollapsible";
import type { PersonaUsageDetail } from "@/types/discussion";
import { DataTable, type DataTableColumn } from "../ui/table";

export interface RoundUsageDetailProps {
  discussionId: string;
  round: number;
  /** Per-persona usage + prompt-composition breakdown for this round. */
  details: PersonaUsageDetail[];
  personaName: (id: string) => string;
}

// Stable display order for the prompt sections (keys come from the
// backend `measure_prompt_breakdown`). Unknown keys append after these.
const SECTION_ORDER = [
  "system",
  "instructions",
  "annotation",
  "freshness",
  "topic_rules",
  "context_json",
  "history",
  "tool_hint",
] as const;

/**
 * Collapsible "ctx 用量明細" panel rendered inside an expanded round.
 * Surfaces three dimensions the user asked for:
 *   1. per-persona exact tokens / tools / cost (from llm_usage_events),
 *   2. per prompt-section size (estimated tokens),
 *   3. per context-block size (estimated tokens, sorted — `focus_briefs`
 *      usually dominates).
 *
 * Exact per-persona numbers come from the provider. Section / block
 * numbers are local char measurements scaled to estimated tokens using
 * the round's real prompt_tokens, so the breakdown sums to the actual
 * input total — labelled "估" to stay honest about the estimation.
 */
export function RoundUsageDetail({
  discussionId,
  round,
  details,
  personaName,
}: RoundUsageDetailProps) {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible(
    `discussion.${discussionId}.round.${round}.usage`,
    false,
  );
  if (details.length === 0) return null;

  // Round-level aggregates across personas. Char counts sum; est tokens
  // scale by (round prompt_tokens / round chars) so the breakdown adds
  // up to the real input total.
  let sumPrompt = 0;
  let sumChars = 0;
  const sectionChars: Record<string, number> = {};
  const blockChars: Record<string, number> = {};
  for (const d of details) {
    sumPrompt += d.prompt_tokens;
    const bd = d.breakdown;
    if (!bd) continue;
    sumChars += bd.total_chars;
    for (const [k, v] of Object.entries(bd.sections)) {
      sectionChars[k] = (sectionChars[k] ?? 0) + v;
    }
    for (const [k, v] of Object.entries(bd.blocks)) {
      blockChars[k] = (blockChars[k] ?? 0) + v;
    }
  }
  const ratio = sumChars > 0 ? sumPrompt / sumChars : 0;
  const estTok = (chars: number) => Math.round(chars * ratio);

  const knownSections = SECTION_ORDER.filter((k) => sectionChars[k]);
  const extraSections = Object.keys(sectionChars).filter(
    (k) => !(SECTION_ORDER as readonly string[]).includes(k),
  );
  const orderedSections = [...knownSections, ...extraSections];
  const maxSection = Math.max(
    1,
    ...orderedSections.map((k) => sectionChars[k] ?? 0),
  );
  const orderedBlocks = Object.entries(blockChars).sort((a, b) => b[1] - a[1]);
  const maxBlock = Math.max(1, ...orderedBlocks.map(([, v]) => v));

  const personaRows = details
    .slice()
    .sort((a, b) => b.prompt_tokens - a.prompt_tokens);
  const personaColumns: DataTableColumn<PersonaUsageDetail>[] = [
    {
      key: "persona",
      header: t("discussion.usage_detail.persona"),
      mobile: "primary",
      cellClassName: "text-foreground",
      render: (d) => personaName(d.persona_id),
    },
    {
      key: "input",
      header: t("discussion.usage_detail.input"),
      numeric: true,
      render: (d) => d.prompt_tokens.toLocaleString(),
    },
    {
      key: "output",
      header: t("discussion.usage_detail.output"),
      numeric: true,
      render: (d) => d.completion_tokens.toLocaleString(),
    },
    {
      key: "tools",
      header: t("discussion.usage_detail.tools"),
      numeric: true,
      render: (d) => d.tool_call_count || "—",
    },
    {
      key: "cost",
      header: t("discussion.usage_detail.cost"),
      numeric: true,
      render: (d) => (d.cost_usd > 0 ? `$${d.cost_usd.toFixed(4)}` : "—"),
    },
  ];

  return (
    <div className="mb-3 rounded-lg border border-border bg-card/50">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="w-full flex items-center justify-between px-3 py-2 text-meta font-medium text-muted-foreground hover:text-foreground"
      >
        <span>{t("discussion.usage_detail.title")}</span>
        <span className="tabular-nums">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-4 text-meta">
          <section>
            <div className="mb-1 font-semibold text-foreground/80">
              {t("discussion.usage_detail.per_persona")}
            </div>
            <DataTable
              columns={personaColumns}
              rows={personaRows}
              rowKey={(d) => d.persona_id}
              mobileMode="cards"
              aria-label={t("discussion.usage_detail.per_persona")}
            />
          </section>

          {orderedSections.length > 0 && (
            <section>
              <div className="mb-1 font-semibold text-foreground/80">
                {t("discussion.usage_detail.by_section")}{" "}
                <span className="font-normal text-muted-foreground">
                  {t("discussion.usage_detail.est")}
                </span>
              </div>
              <div className="space-y-1">
                {orderedSections.map((k) => (
                  <BreakdownBar
                    key={k}
                    label={t(`discussion.usage_detail.section.${k}`, {
                      defaultValue: k,
                    })}
                    chars={sectionChars[k] ?? 0}
                    estTokens={estTok(sectionChars[k] ?? 0)}
                    pct={((sectionChars[k] ?? 0) / maxSection) * 100}
                  />
                ))}
              </div>
            </section>
          )}

          {orderedBlocks.length > 0 && (
            <section>
              <div className="mb-1 font-semibold text-foreground/80">
                {t("discussion.usage_detail.by_block")}{" "}
                <span className="font-normal text-muted-foreground">
                  {t("discussion.usage_detail.est")}
                </span>
              </div>
              <div className="space-y-1">
                {orderedBlocks.map(([k, v]) => (
                  <BreakdownBar
                    key={k}
                    label={k}
                    chars={v}
                    estTokens={estTok(v)}
                    pct={(v / maxBlock) * 100}
                  />
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function BreakdownBar({
  label,
  chars,
  estTokens,
  pct,
}: {
  label: string;
  chars: number;
  estTokens: number;
  pct: number;
}) {
  return (
    <div className="flex items-center gap-2">
      <span
        className="w-28 shrink-0 truncate text-muted-foreground"
        title={label}
      >
        {label}
      </span>
      <span className="relative flex-1 h-3 rounded bg-muted overflow-hidden">
        <span
          className="absolute inset-y-0 left-0 rounded bg-primary/40"
          style={{ width: `${Math.max(2, pct)}%` }}
        />
      </span>
      <span className="w-24 shrink-0 text-right tabular-nums text-muted-foreground">
        {estTokens.toLocaleString()} · {chars.toLocaleString()}c
      </span>
    </div>
  );
}
