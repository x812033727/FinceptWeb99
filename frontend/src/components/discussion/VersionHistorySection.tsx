import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { formatTaipei } from "@/lib/timeFormat";
import { DataTable, type DataTableColumn } from "../ui/table";
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

  const columns: DataTableColumn<StrategyVersionRow>[] = [
    {
      key: "v",
      header: "v",
      mobile: "primary",
      render: (v) => <span className="font-mono">v{v.version_number}</span>,
    },
    {
      key: "kind",
      header: t("discussion.versions.col.kind"),
      render: (v) => (
        <span
          className={`px-1.5 py-0.5 text-micro rounded border ${
            v.artifact_kind === "weights"
              ? "border-blue-700/40 text-blue-300"
              : "border-purple-700/40 text-purple-300"
          }`}
        >
          {t(`discussion.versions.kind.${v.artifact_kind}`)}
        </span>
      ),
    },
    {
      key: "fit_at",
      header: t("discussion.versions.col.fit_at"),
      render: (v) => (
        <span className="text-muted-foreground">
          {v.fit_at ? formatTaipei(v.fit_at, "date") : "—"}
        </span>
      ),
    },
    {
      key: "samples",
      header: t("discussion.versions.col.samples"),
      numeric: true,
      render: (v) => <>{v.sample_count ?? "—"}</>,
    },
    {
      key: "status",
      header: t("discussion.versions.col.status"),
      render: (v) => (
        <span
          className={`text-micro ${
            v.status === "active"
              ? "text-success"
              : v.status === "rolled_back"
                ? "text-danger"
                : "text-muted-foreground"
          }`}
        >
          {t(`discussion.versions.status.${v.status}`)}
        </span>
      ),
    },
    {
      key: "action",
      header: "",
      align: "right",
      render: (v) =>
        v.status !== "active" ? (
          <button
            type="button"
            onClick={() => rollbackMut.mutate(v.id)}
            disabled={rollbackMut.isPending}
            className="px-2 py-0.5 text-micro border border-border
                       text-muted-foreground rounded
                       hover:text-warning hover:border-warning/30
                       disabled:opacity-50"
          >
            {t("discussion.versions.rollback")}
          </button>
        ) : null,
    },
  ];

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
                className={`px-2 py-0.5 text-micro rounded border ${
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
            <DataTable
              columns={columns}
              rows={versions}
              rowKey={(v) => v.id}
              mobileMode="cards"
              className="text-[11px]"
              aria-label={t("discussion.versions.title")}
            />
          )}
        </div>
      )}
    </div>
  );
}
