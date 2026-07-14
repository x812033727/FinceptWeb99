import { useTranslation } from "react-i18next";
import { formatProgressPct } from "@/lib/formatters";
import type { PerDatasetProgress } from "@/hooks/useFinmindChain";

// Pure-move out of FinmindBackfillCard (W-final G8) to keep the parent card
// under the 500-line threshold. Behaviour identical.

export function PerDatasetTable({
  rows,
  currentDataset,
}: {
  rows: PerDatasetProgress[];
  currentDataset: string | null;
}) {
  const { t } = useTranslation();
  if (rows.length === 0) return null;
  return (
    <div>
      <div className="mb-1 flex justify-between text-sm">
        <span className="font-semibold">{t("admin.finmindBackfill.per_dataset_progress")}</span>
        <span className="text-xs text-muted-foreground">
          {t("admin.finmindBackfill.row_estimate_note")}
        </span>
      </div>
      <div className="overflow-x-auto rounded border border-border">
        <table className="w-full text-xs">
          <thead className="bg-muted/40 text-muted-foreground">
            <tr>
              <th className="px-2 py-1 text-left font-medium">Dataset</th>
              <th className="px-2 py-1 text-right font-medium">Chunks</th>
              <th className="px-2 py-1 text-right font-medium">%</th>
              <th className="px-2 py-1 text-right font-medium">{t("admin.finmindBackfill.th_failed")}</th>
              <th className="px-2 py-1 text-right font-medium">{t("admin.finmindBackfill.th_table_rows")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const pct = formatProgressPct(r.chunks_done, r.chunks_total);
              const isCurrent = r.dataset === currentDataset;
              const fullyDone =
                r.chunks_total > 0 && r.chunks_done >= r.chunks_total;
              return (
                <tr
                  key={r.dataset}
                  className={`border-t border-border ${
                    isCurrent ? "bg-primary/10" : ""
                  }`}
                >
                  <td className="px-2 py-1 font-mono">
                    {r.dataset}
                    {r.local_table && (
                      <span className="ml-1 text-muted-foreground">
                        → {r.local_table}
                      </span>
                    )}
                  </td>
                  <td className="px-2 py-1 text-right font-mono">
                    {r.chunks_done.toLocaleString()}/
                    {r.chunks_total.toLocaleString()}
                  </td>
                  <td
                    className={`px-2 py-1 text-right font-mono ${
                      fullyDone ? "text-success" : ""
                    }`}
                  >
                    {pct}
                  </td>
                  <td
                    className={`px-2 py-1 text-right font-mono ${
                      r.chunks_failed > 0 ? "text-destructive" : "text-muted-foreground"
                    }`}
                  >
                    {r.chunks_failed.toLocaleString()}
                  </td>
                  <td className="px-2 py-1 text-right font-mono">
                    {r.row_count == null
                      ? "—"
                      : `≈ ${r.row_count.toLocaleString()}`}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
