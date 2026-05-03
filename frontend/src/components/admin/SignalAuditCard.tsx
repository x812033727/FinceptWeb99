import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";
import { CollapsibleHeader, useCollapsible } from "@/components/Collapsible";

/**
 * Audit which discussion-context signals personas are actually citing.
 *
 * Backend: GET /api/admin/signal-audit?recent=N&market=X (PR #242)
 * The endpoint joins `discussion_round_contexts` × `discussion_turns`,
 * scans each persona's content for keyword matches per signal, and
 * rolls up a citation-rate table sorted ascending so zero-uptake
 * offenders surface at the top of the list.
 *
 * UI: horizontal bar chart (ASCII-style block bars rendered with CSS
 * width — no Recharts dep needed, 4 lines of code, looks clean) plus
 * a prominent zero-uptake callout. Auto-refresh disabled — admin
 * tool, not a live dashboard.
 */

interface CoverageRow {
  signal: string;
  present: number;
  cited: number;
  /** PR #258: subset of `cited` where the persona's content also
   *  carried a numeric token within ±60 chars of the keyword.
   *  Always ≤ `cited`; older backends without the field send 0. */
  cited_with_value?: number;
  persona_count: number;
  citation_rate: number;
  /** PR #258 sibling rate. 0 when the backend doesn't surface it. */
  value_citation_rate?: number;
}

interface HallucinationRow {
  signal: string;
  absent_rounds: number;
  hallucinated: number;
  persona_count_absent: number;
  hallucination_rate: number;
}

interface SignalAuditResp {
  discussions_audited: number;
  discussion_ids: string[];
  coverage: CoverageRow[];
  zero_uptake: string[];
  /** PR #259: signals personas cited with a value despite the round
   *  prompt not containing them. Older backends without the field
   *  send undefined → frontend treats as empty list. */
  hallucinations?: HallucinationRow[];
}

const RECENT_OPTIONS = [10, 30, 50, 100];
const MARKET_OPTIONS: Array<{ value: string | undefined; label: string }> = [
  { value: undefined, label: "All" },
  { value: "TW", label: "TW" },
  { value: "US", label: "US" },
  { value: "GLOBAL", label: "GLOBAL" },
];

/**
 * Bar-fill colour for a citation-rate. Mirrors the audit CLI's mental
 * model so admins reading the bulk script + the AdminPage card see
 * the same severity bins:
 *
 *   < 10 %  critical / near-zero uptake → red
 *   < 50 %  under-utilised              → yellow
 *  ≥ 50 %  healthy                      → emerald
 *
 * Exported for direct unit tests; the thresholds are policy and
 * shouldn't drift between the component and any future consumers
 * (e.g. a sparkline trend view).
 */
export function rateColor(rate: number): string {
  if (rate < 0.1) return "bg-red-500/70";
  if (rate < 0.5) return "bg-yellow-500/70";
  return "bg-emerald-500/70";
}

/**
 * Format a 0–1 citation rate as a percentage string with one
 * decimal. `0` → "0.0%", `1` → "100.0%", `0.6383` → "63.8%".
 */
export function rateLabel(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

export function SignalAuditCard() {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible("signal-audit");
  const [recent, setRecent] = useState(30);
  const [market, setMarket] = useState<string | undefined>(undefined);

  const { data, isLoading, refetch, isFetching } = useQuery<SignalAuditResp>({
    queryKey: ["signal-audit", recent, market],
    queryFn: () => {
      const qs = new URLSearchParams({ recent: String(recent) });
      if (market) qs.set("market", market);
      return api.get(`/admin/signal-audit?${qs.toString()}`).then((r) => r.data);
    },
    // Audit is read-heavy on the DB — don't auto-refetch on focus.
    refetchOnWindowFocus: false,
    enabled: open,
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open}
        toggle={toggle}
        title={t("signal_audit.title")}
        subtitle={t("signal_audit.subtitle")}
        headerRight={
          open ? (
            <div
              className="flex items-center gap-2"
              onClick={(e) => e.stopPropagation()}
            >
              <select
                value={market ?? ""}
                onChange={(e) => setMarket(e.target.value || undefined)}
                className="bg-secondary/30 border border-border rounded px-2 py-1 text-xs"
              >
                {MARKET_OPTIONS.map((m) => (
                  <option key={m.label} value={m.value ?? ""}>
                    {m.label}
                  </option>
                ))}
              </select>
              <div className="flex gap-1">
                {RECENT_OPTIONS.map((n) => (
                  <button
                    key={n}
                    onClick={() => setRecent(n)}
                    className={`px-2.5 py-1 text-xs rounded ${
                      recent === n
                        ? "bg-primary text-primary-foreground"
                        : "border border-border text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    N={n}
                  </button>
                ))}
              </div>
              <button
                onClick={() => refetch()}
                disabled={isFetching}
                className="px-2 py-1 text-xs rounded border border-border text-muted-foreground hover:text-foreground disabled:opacity-50"
                aria-label={t("signal_audit.refresh")}
              >
                ↻
              </button>
            </div>
          ) : null
        }
      />

      {open && (
        <>
          {isLoading || !data ? (
            <p className="text-xs text-muted-foreground animate-pulse">
              {t("common.loading")}
            </p>
          ) : data.discussions_audited === 0 ? (
            <p className="text-xs text-muted-foreground py-3">
              {t("signal_audit.empty")}
            </p>
          ) : (
            <>
              <div className="flex items-center gap-3 text-xs text-muted-foreground">
                <span>
                  {t("signal_audit.audited", {
                    count: data.discussions_audited,
                    requested: recent,
                  })}
                </span>
                {data.zero_uptake.length > 0 && (
                  <span className="text-red-400">
                    ⚠ {t("signal_audit.zero_uptake_count", {
                      count: data.zero_uptake.length,
                    })}
                  </span>
                )}
                {(data.hallucinations?.length ?? 0) > 0 && (
                  <span className="text-orange-400">
                    ✗ {t("signal_audit.hallucination_count", {
                      count: data.hallucinations?.length ?? 0,
                    })}
                  </span>
                )}
              </div>

              {data.zero_uptake.length > 0 && (
                <div className="bg-red-500/5 border border-red-500/30 rounded p-2 space-y-1">
                  <p className="text-[11px] font-semibold text-red-400 uppercase tracking-wider">
                    {t("signal_audit.zero_uptake_title")}
                  </p>
                  <p className="text-[11px] text-muted-foreground">
                    {t("signal_audit.zero_uptake_help")}
                  </p>
                  <ul className="text-xs font-mono space-y-0.5">
                    {data.zero_uptake.map((sig) => (
                      <li key={sig} className="text-red-300">
                        {sig}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {(data.hallucinations?.length ?? 0) > 0 && (
                <div className="bg-orange-500/5 border border-orange-500/30 rounded p-2 space-y-1">
                  <p className="text-[11px] font-semibold text-orange-400 uppercase tracking-wider">
                    {t("signal_audit.hallucination_title")}
                  </p>
                  <p className="text-[11px] text-muted-foreground">
                    {t("signal_audit.hallucination_help")}
                  </p>
                  <ul className="text-xs font-mono space-y-0.5">
                    {data.hallucinations?.map((row) => (
                      <li
                        key={row.signal}
                        className="text-orange-300 flex justify-between gap-2"
                        title={`${row.hallucinated} turns / ${row.persona_count_absent} absent persona-turns across ${row.absent_rounds} rounds`}
                      >
                        <span className="truncate">{row.signal}</span>
                        <span className="tabular-nums text-orange-400/80 shrink-0">
                          {rateLabel(row.hallucination_rate)} ({row.hallucinated}/{row.persona_count_absent})
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="space-y-1.5">
                <p className="text-[10px] text-muted-foreground uppercase tracking-wider">
                  {t("signal_audit.coverage_title")}
                </p>
                <ul className="space-y-1">
                  {data.coverage.map((row) => (
                    <li
                      key={row.signal}
                      className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto_auto] items-center gap-3 text-xs"
                      title={`${row.cited} / ${row.persona_count} cited across ${row.present} round-snapshots`}
                    >
                      <span className="font-mono truncate" title={row.signal}>
                        {row.signal}
                      </span>
                      <div
                        className="h-2 bg-secondary/40 rounded overflow-hidden relative"
                        role="img"
                        aria-label={`citation rate ${rateLabel(row.citation_rate)}, value-citation rate ${rateLabel(row.value_citation_rate ?? 0)}`}
                      >
                        {/* Outer bar = keyword citation rate.
                            Inner darker bar = subset that ALSO
                            quoted a number (`cited_with_value`,
                            PR #258). Stacking visualises the
                            "name-drop vs concrete value" gap at a
                            glance. */}
                        <div
                          className={`h-full ${rateColor(row.citation_rate)}`}
                          style={{
                            width: `${Math.max(row.citation_rate * 100, 1)}%`,
                          }}
                        />
                        <div
                          className="absolute top-0 left-0 h-full bg-foreground/30"
                          style={{
                            width: `${Math.max((row.value_citation_rate ?? 0) * 100, 0)}%`,
                          }}
                        />
                      </div>
                      <span
                        className="font-mono tabular-nums text-right w-20"
                        title={t("signal_audit.cite_value_split", {
                          cite: rateLabel(row.citation_rate),
                          value: rateLabel(row.value_citation_rate ?? 0),
                        })}
                      >
                        {rateLabel(row.citation_rate)}
                        <span className="text-muted-foreground/70">
                          {" / "}
                          {rateLabel(row.value_citation_rate ?? 0)}
                        </span>
                      </span>
                      <span className="font-mono tabular-nums text-muted-foreground text-right w-16">
                        {row.cited}/{row.persona_count}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
