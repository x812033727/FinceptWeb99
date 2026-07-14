import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";

interface ArchivedLessonRow {
  id: number;
  market: string;
  category: string;
  lesson_text: string;
  tier: string;
  usage_count: number;
  hit_count: number;
  recent_hit_rate_10: number | null;
  archived_at: string | null;
  created_at: string;
}

/**
 * PR-4c: AdminPage card listing soft-deleted lessons that the
 * `archive_stale_lessons` cron path discarded. Operators can:
 *   - inspect WHY a lesson got archived (low hit rate or
 *     unused-for-60d AND zero hits)
 *   - manually restore one if the archive was wrong
 *
 * Hides itself entirely when the archive is empty so the
 * AdminPage doesn't grow noise on a fresh deployment.
 */
export function LessonArchiveCard() {
  const { open, toggle } = useCollapsible("admin.lesson-archive");
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [marketFilter, setMarketFilter] = useState<string>("");

  const archiveQ = useQuery({
    queryKey: ["admin", "lesson-archive", marketFilter],
    queryFn: async () => {
      const params = marketFilter ? `?market=${marketFilter}` : "";
      const { data } = await api.get<{
        items: ArchivedLessonRow[];
        limit: number;
        offset: number;
      }>(`/discussion/lessons/archived${params}`);
      return data;
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const unarchiveMut = useMutation({
    mutationFn: async (lessonId: number) => {
      const { data } = await api.post(
        `/discussion/lessons/${lessonId}/unarchive`,
      );
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "lesson-archive"] });
    },
  });

  const items = archiveQ.data?.items ?? [];
  if (!archiveQ.isLoading && items.length === 0) return null;

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title={t("admin.lesson_archive.title")}
        subtitle={
          archiveQ.isLoading ? (
            <span className="animate-pulse">{t("common.loading")}</span>
          ) : (
            <span>{t("admin.lesson_archive.count", { count: items.length })}</span>
          )
        }
      />
      {open && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <label className="text-meta text-muted-foreground">
              {t("admin.lesson_archive.market_filter")}
            </label>
            <select
              value={marketFilter}
              onChange={(e) => setMarketFilter(e.target.value)}
              className="bg-background border border-border rounded px-2 py-0.5 text-meta"
            >
              <option value="">{t("admin.lesson_archive.market_all")}</option>
              <option value="TW">TW</option>
              <option value="US">US</option>
              <option value="GLOBAL">GLOBAL</option>
            </select>
          </div>
          {items.length === 0 ? (
            <div className="text-meta text-muted-foreground italic">
              {t("admin.lesson_archive.empty")}
            </div>
          ) : (
            <ul className="space-y-1.5">
              {items.map((row) => (
                <li
                  key={row.id}
                  className="border border-border rounded p-2 text-meta flex items-start justify-between gap-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5 text-micro text-muted-foreground mb-0.5">
                      <span className="font-mono">{row.market}</span>
                      <span>·</span>
                      <span>{row.tier}</span>
                      <span>·</span>
                      <span>{row.category}</span>
                      <span>·</span>
                      <span>
                        {t("admin.lesson_archive.usage_label")}:{" "}
                        {row.hit_count}/{row.usage_count}
                      </span>
                      {row.recent_hit_rate_10 !== null && (
                        <>
                          <span>·</span>
                          <span>
                            r10:{" "}
                            {(row.recent_hit_rate_10 * 100).toFixed(0)}%
                          </span>
                        </>
                      )}
                    </div>
                    <div className="text-foreground truncate">
                      {row.lesson_text}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => unarchiveMut.mutate(row.id)}
                    disabled={unarchiveMut.isPending}
                    className="px-2 py-0.5 text-micro border border-border
                               text-muted-foreground rounded
                               hover:text-success hover:border-success/30
                               disabled:opacity-50 shrink-0"
                  >
                    {t("admin.lesson_archive.unarchive")}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
