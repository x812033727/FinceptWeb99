import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { formatTaipei } from "@/lib/timeFormat";
import type { StrategyVersionRow } from "./_helpers";

/**
 * PR-4a: collapsible drawer listing past fits of this strategy's
 * weights / calibration curve, with a one-click rollback action
 * per row.
 *
 * Keeps the UI cheap: lazy-loads only when the operator opens it
 * (no bandwidth tax on the typical "I'm just looking at the
 * strategy" read path), and the whole drawer hides until the
 * strategy actually has versions to show.
 */
export function VersionHistorySection({ strategyId }: { strategyId: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [kindFilter, setKindFilter] =
    useState<"all" | "weights" | "calibration_curve">("all");

  const qc = useQueryClient();
  const versionsQ = useQuery({
    queryKey: ["strategy-versions", strategyId, kindFilter],
    enabled: open,
    queryFn: async () => {
      const params = kindFilter === "all"
        ? ""
        : `?artifact_kind=${kindFilter}`;
      const { data } = await api.get<StrategyVersionRow[]>(
        `/discussion/strategies/${strategyId}/versions${params}`,
      );
      return data;
    },
    staleTime: 30_000,
  });

  const rollbackMut = useMutation({
    mutationFn: async (versionId: string) => {
      const { data } = await api.post(
        `/discussion/strategies/${strategyId}/versions/${versionId}/rollback`,
      );
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategy-versions", strategyId] });
      qc.invalidateQueries({ queryKey: ["strategies"] });
    },
  });

  const versions = versionsQ.data ?? [];

  return (
    <div className="border-t border-border/50 mt-2 pt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-[11px] text-muted-foreground hover:text-foreground
                   flex items-center gap-1"
      >
        <span className="w-3 inline-block">{open ? "▼" : "▶"}</span>
        {t("discussion.versions.title")}
      </button>

      {open && (
        <div className="mt-2 space-y-2">
          <div className="flex gap-1.5">
            {(["all", "weights", "calibration_curve"] as const).map((k) => (
              <button
                key={k}
                type="button"
                onClick={() => setKindFilter(k)}
                className={`px-2 py-0.5 text-[10px] rounded border ${
                  kindFilter === k
                    ? "bg-primary/20 text-primary border-primary/40"
                    : "border-border text-muted-foreground hover:text-foreground"
                }`}
              >
                {t(`discussion.versions.kind.${k}`)}
              </button>
            ))}
          </div>

          {versionsQ.isLoading && (
            <div className="text-[11px] text-muted-foreground animate-pulse">
              {t("common.loading")}
            </div>
          )}
          {!versionsQ.isLoading && versions.length === 0 && (
            <div className="text-[11px] text-muted-foreground italic">
              {t("discussion.versions.empty")}
            </div>
          )}
          {versions.length > 0 && (
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-muted-foreground border-b border-border">
                  <th className="text-left py-1">v</th>
                  <th className="text-left py-1">{t("discussion.versions.col.kind")}</th>
                  <th className="text-left py-1">{t("discussion.versions.col.fit_at")}</th>
                  <th className="text-right py-1">{t("discussion.versions.col.samples")}</th>
                  <th className="text-left py-1">{t("discussion.versions.col.status")}</th>
                  <th className="text-right py-1"></th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => {
                  const isActive = v.status === "active";
                  const fitAt = v.fit_at
                    ? formatTaipei(v.fit_at, "date")
                    : "—";
                  return (
                    <tr key={v.id} className="border-b border-border/40">
                      <td className="py-1 font-mono">v{v.version_number}</td>
                      <td className="py-1">
                        <span
                          className={`px-1.5 py-0.5 text-[10px] rounded border ${
                            v.artifact_kind === "weights"
                              ? "border-blue-700/40 text-blue-300"
                              : "border-purple-700/40 text-purple-300"
                          }`}
                        >
                          {t(`discussion.versions.kind.${v.artifact_kind}`)}
                        </span>
                      </td>
                      <td className="py-1 text-muted-foreground">{fitAt}</td>
                      <td className="py-1 text-right tabular-nums">
                        {v.sample_count ?? "—"}
                      </td>
                      <td className="py-1">
                        <span
                          className={`text-[10px] ${
                            isActive
                              ? "text-emerald-300"
                              : v.status === "rolled_back"
                                ? "text-red-300"
                                : "text-muted-foreground"
                          }`}
                        >
                          {t(`discussion.versions.status.${v.status}`)}
                        </span>
                      </td>
                      <td className="py-1 text-right">
                        {!isActive && (
                          <button
                            type="button"
                            onClick={() => rollbackMut.mutate(v.id)}
                            disabled={rollbackMut.isPending}
                            className="px-2 py-0.5 text-[10px] border border-border
                                       text-muted-foreground rounded
                                       hover:text-amber-300 hover:border-amber-700/40
                                       disabled:opacity-50"
                          >
                            {t("discussion.versions.rollback")}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
