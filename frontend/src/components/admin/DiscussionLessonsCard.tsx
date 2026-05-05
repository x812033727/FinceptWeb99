import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import { CollapsibleHeader, useCollapsible } from "@/components/Collapsible";

/**
 * AdminPage card for browsing + pruning the post-mortem learning
 * archive. Owner-scoped — admin sees their own bucket only (a
 * future enhancement could expose an `all_users` toggle when
 * role == admin).
 *
 * Backend:
 *   GET    /api/discussion/lessons?market=&limit=
 *   DELETE /api/discussion/lessons/{id}
 *
 * The card is collapsed by default — most days the operator
 * doesn't need to look at this; it's a tool for triaging when a
 * specific lesson appears to be misleading the personas.
 */

interface LessonRow {
  id: number;
  discussion_id: string;
  market: string;
  as_of_date: string;
  category: string;
  lesson_text: string;
  related_symbols: string[];
  missed_winners: string[];
  created_at: string | null;
}

const MARKET_OPTIONS: Array<{ value: string | undefined; label: string }> = [
  { value: undefined, label: "All" },
  { value: "TW", label: "TW" },
  { value: "US", label: "US" },
  { value: "GLOBAL", label: "GLOBAL" },
];

const LIMIT_OPTIONS = [25, 50, 100];

const CATEGORY_BADGE: Record<string, string> = {
  missed_sector: "bg-amber-900/40 text-amber-200",
  wrong_signal_weight: "bg-purple-900/40 text-purple-200",
  missing_data: "bg-blue-900/40 text-blue-200",
  over_confidence: "bg-red-900/40 text-red-200",
  other: "bg-secondary text-muted-foreground",
};

export function DiscussionLessonsCard() {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible("discussion-lessons");
  const [market, setMarket] = useState<string | undefined>(undefined);
  const [limit, setLimit] = useState<number>(50);
  const queryClient = useQueryClient();

  const { data, isLoading, refetch, isFetching } = useQuery<LessonRow[]>({
    queryKey: ["discussion-lessons", market, limit],
    queryFn: () => {
      const qs = new URLSearchParams({ limit: String(limit) });
      if (market) qs.set("market", market);
      return api
        .get<LessonRow[]>(`/discussion/lessons?${qs.toString()}`)
        .then((r) => r.data);
    },
    refetchOnWindowFocus: false,
    enabled: open,
  });

  async function handleDelete(id: number) {
    if (!window.confirm(t("discussion_lessons.confirm_delete"))) return;
    try {
      await api.delete(`/discussion/lessons/${id}`);
      await queryClient.invalidateQueries({
        queryKey: ["discussion-lessons"],
      });
    } catch (err) {
      // Surface the failure inline rather than blocking other deletes
      console.error("delete lesson failed", err);
    }
  }

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open}
        toggle={toggle}
        title={t("discussion_lessons.title")}
        subtitle={t("discussion_lessons.subtitle")}
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
              <select
                value={String(limit)}
                onChange={(e) => setLimit(Number(e.target.value))}
                className="bg-secondary/30 border border-border rounded px-2 py-1 text-xs"
              >
                {LIMIT_OPTIONS.map((n) => (
                  <option key={n} value={n}>
                    N={n}
                  </option>
                ))}
              </select>
              <button
                onClick={() => refetch()}
                disabled={isFetching}
                className="px-2 py-1 text-xs rounded border border-border text-muted-foreground hover:text-foreground disabled:opacity-50"
                aria-label={t("discussion_lessons.refresh")}
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
          ) : data.length === 0 ? (
            <p className="text-xs text-muted-foreground py-3">
              {t("discussion_lessons.empty")}
            </p>
          ) : (
            <ul className="space-y-2">
              {data.map((row) => (
                <li
                  key={row.id}
                  className="border border-border rounded p-2 space-y-1"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                      <span className="font-mono">{row.market}</span>
                      <span>·</span>
                      <span className="font-mono">{row.as_of_date}</span>
                      <span>·</span>
                      <span
                        className={`px-1.5 py-0.5 rounded text-[10px] ${
                          CATEGORY_BADGE[row.category] ?? CATEGORY_BADGE.other
                        }`}
                      >
                        {row.category}
                      </span>
                    </div>
                    <button
                      onClick={() => handleDelete(row.id)}
                      className="text-xs px-2 py-0.5 rounded border border-red-800/50 text-red-300 hover:bg-red-900/30"
                    >
                      {t("discussion_lessons.delete")}
                    </button>
                  </div>
                  <p className="text-sm text-foreground whitespace-pre-wrap">
                    {row.lesson_text}
                  </p>
                  {(row.related_symbols.length > 0 ||
                    row.missed_winners.length > 0) && (
                    <div className="flex flex-wrap gap-1 text-[10px] text-muted-foreground">
                      {row.related_symbols.length > 0 && (
                        <span>
                          {t("discussion_lessons.related")}:{" "}
                          {row.related_symbols.join(", ")}
                        </span>
                      )}
                      {row.missed_winners.length > 0 && (
                        <span>
                          {t("discussion_lessons.missed")}:{" "}
                          {row.missed_winners.join(", ")}
                        </span>
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
