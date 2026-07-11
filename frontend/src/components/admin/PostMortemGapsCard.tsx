import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";

/**
 * Aggregate "what data was missing" mentions extracted from
 * post-mortem persona reflections (PR #249 self-critique flow).
 *
 * Backend: GET /api/admin/post-mortem-gaps?recent=N&market=X (PR #262)
 * The endpoint scans `discussion_turns` for post-mortem rounds (turns
 * that come after a `_user`/`user_input` injection whose content
 * starts with "【事後檢討"), keyword-matches each persona answer
 * against a curated taxonomy of data categories the platform doesn't
 * yet provide, and rolls up the most-mentioned gaps as a prioritized
 * "what to build next" backlog.
 *
 * UI: horizontal bar chart sized by raw mention count, plus a per-row
 * (mentions / discussions / personas) split so the operator can tell
 * a one-discussion shouting match from a broad-based theme.
 */

interface GapRow {
  category: string;
  mentions: number;
  discussions_mentioning: number;
  persona_count_mentioning: number;
}

interface PostMortemGapsResp {
  discussions_audited: number;
  discussion_ids: string[];
  gaps: GapRow[];
}

const RECENT_OPTIONS = [10, 30, 50, 100];
const MARKET_OPTIONS: Array<{ value: string | undefined; label: string }> = [
  { value: undefined, label: "All" },
  { value: "TW", label: "TW" },
  { value: "US", label: "US" },
  { value: "GLOBAL", label: "GLOBAL" },
];

export function PostMortemGapsCard() {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible("post-mortem-gaps");
  const [recent, setRecent] = useState(30);
  const [market, setMarket] = useState<string | undefined>(undefined);

  const { data, isLoading, refetch, isFetching } =
    useQuery<PostMortemGapsResp>({
      queryKey: ["post-mortem-gaps", recent, market],
      queryFn: () => {
        const qs = new URLSearchParams({ recent: String(recent) });
        if (market) qs.set("market", market);
        return api
          .get(`/admin/post-mortem-gaps?${qs.toString()}`)
          .then((r) => r.data);
      },
      refetchOnWindowFocus: false,
      enabled: open,
    });

  const maxMentions = Math.max(1, ...(data?.gaps.map((g) => g.mentions) ?? [0]));

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open}
        toggle={toggle}
        title={t("post_mortem_gaps.title")}
        subtitle={t("post_mortem_gaps.subtitle")}
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
                aria-label={t("post_mortem_gaps.refresh")}
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
              {t("post_mortem_gaps.empty")}
            </p>
          ) : data.gaps.length === 0 ? (
            <p className="text-xs text-muted-foreground py-3">
              {t("post_mortem_gaps.no_hits", {
                count: data.discussions_audited,
              })}
            </p>
          ) : (
            <>
              <p className="text-xs text-muted-foreground">
                {t("post_mortem_gaps.audited", {
                  count: data.discussions_audited,
                  requested: recent,
                })}
              </p>
              <ul className="space-y-1">
                {data.gaps.map((row) => (
                  <li
                    key={row.category}
                    className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto] items-center gap-3 text-xs"
                    title={t("post_mortem_gaps.row_tooltip", {
                      mentions: row.mentions,
                      discussions: row.discussions_mentioning,
                      personas: row.persona_count_mentioning,
                    })}
                  >
                    <span className="font-mono truncate" title={row.category}>
                      {row.category}
                    </span>
                    <div
                      className="h-2 bg-secondary/40 rounded overflow-hidden"
                      role="img"
                      aria-label={`${row.mentions} mentions across ${row.discussions_mentioning} discussions`}
                    >
                      <div
                        className="h-full bg-warning/70"
                        style={{
                          width: `${Math.max((row.mentions / maxMentions) * 100, 2)}%`,
                        }}
                      />
                    </div>
                    <span className="font-mono tabular-nums text-right w-28 text-muted-foreground">
                      {row.mentions}
                      <span className="text-muted-foreground/60">
                        {` (${row.discussions_mentioning}d / ${row.persona_count_mentioning}p)`}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </div>
  );
}
