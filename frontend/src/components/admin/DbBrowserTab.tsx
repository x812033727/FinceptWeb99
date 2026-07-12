import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Database, Table2, Lock, ArrowLeft } from "lucide-react";
import api from "@/lib/api";
import { DataTable, type DataTableColumn } from "@/components/ui/table";
import { EmptyState } from "@/components/ui/EmptyState";
import { Num } from "@/components/Num";

/**
 * Read-only database browser (AdminPage「DB」tab). Left: the table
 * catalog with row-estimate + size. Click a table → paginated,
 * filterable, orderable row view. All heavy lifting + safety live in
 * the backend `/api/admin/db/*` endpoints; this is a thin viewer.
 */

interface TableInfo {
  schema_name: string;
  table: string;
  row_estimate: number | null;
  total_bytes: number | null;
}

interface ColumnInfo {
  name: string;
  type: string;
  masked: boolean;
}

interface RowsPage {
  schema_name: string;
  table: string;
  columns: ColumnInfo[];
  rows: (string | number | boolean | null)[][];
  page: number;
  page_size: number;
  total: number | null;
  latest_at: string | null;
}

function fmtBytes(n: number | null): string {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function TableCatalog({ onPick }: { onPick: (t: TableInfo) => void }) {
  const { t } = useTranslation();
  const { data = [], isLoading } = useQuery<TableInfo[]>({
    queryKey: ["db-tables"],
    queryFn: () => api.get("/admin/db/tables").then((r) => r.data),
  });

  const columns: DataTableColumn<TableInfo>[] = [
    {
      key: "table",
      header: t("dbBrowser.table"),
      render: (r) => (
        <span className="inline-flex items-center gap-1.5 font-medium">
          <Table2 className="h-3.5 w-3.5 text-muted-foreground/60" aria-hidden="true" />
          {r.schema_name === "public" ? r.table : `${r.schema_name}.${r.table}`}
        </span>
      ),
      mobile: "primary",
    },
    {
      key: "row_estimate",
      header: t("dbBrowser.rows"),
      numeric: true,
      render: (r) => <Num value={r.row_estimate} format="compact" />,
    },
    {
      key: "total_bytes",
      header: t("dbBrowser.size"),
      numeric: true,
      render: (r) => fmtBytes(r.total_bytes),
    },
  ];

  if (isLoading) {
    return <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>;
  }

  return (
    <DataTable
      aria-label={t("dbBrowser.catalog")}
      columns={columns}
      rows={data}
      rowKey={(r) => `${r.schema_name}.${r.table}`}
      mobileMode="cards"
      onRowClick={onPick}
      empty={<EmptyState icon={Database} title={t("dbBrowser.noTables")} />}
    />
  );
}

function TableViewer({ info, onBack }: { info: TableInfo; onBack: () => void }) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const pageSize = 50;
  const { data, isLoading } = useQuery<RowsPage>({
    queryKey: ["db-rows", info.schema_name, info.table, page],
    queryFn: () =>
      api
        .get(`/admin/db/tables/${info.schema_name}/${info.table}/rows`, {
          params: { page, page_size: pageSize },
        })
        .then((r) => r.data),
  });

  const columns = useMemo<DataTableColumn<(string | number | boolean | null)[]>[]>(() => {
    if (!data) return [];
    return data.columns.map((col, i) => ({
      key: col.name,
      header: (
        <span className="inline-flex items-center gap-1">
          {col.name}
          {col.masked && <Lock className="h-3 w-3 text-muted-foreground/50" aria-hidden="true" />}
        </span>
      ),
      render: (row) => {
        const v = row[i];
        return v === null ? <span className="text-muted-foreground/50">null</span> : String(v);
      },
      cellClassName: "font-mono text-[11px] max-w-[240px] truncate",
    }));
  }, [data]);

  const total = data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <button
          onClick={onBack}
          className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
          {t("dbBrowser.backToTables")}
        </button>
        <span className="text-xs text-muted-foreground">
          {info.schema_name === "public" ? info.table : `${info.schema_name}.${info.table}`}
          {data?.latest_at && (
            <span className="ml-2 text-muted-foreground/60">· {t("dbBrowser.latest")} {data.latest_at}</span>
          )}
        </span>
      </div>

      {isLoading || !data ? (
        <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>
      ) : (
        <>
          <DataTable
            aria-label={info.table}
            columns={columns}
            rows={data.rows}
            rowKey={(_row, i) => i}
            mobileMode="scroll"
            stickyHeader
            empty={<EmptyState icon={Database} title={t("dbBrowser.noRows")} />}
          />
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>
              {t("dbBrowser.total")}: <Num value={total} format="compact" className="text-foreground" />
            </span>
            <div className="flex items-center gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                className="rounded border border-border px-2 py-1 disabled:opacity-40 hover:text-foreground"
              >
                {t("common.prev")}
              </button>
              <span className="tabular-nums">{page} / {pageCount}</span>
              <button
                disabled={page >= pageCount}
                onClick={() => setPage((p) => p + 1)}
                className="rounded border border-border px-2 py-1 disabled:opacity-40 hover:text-foreground"
              >
                {t("common.next")}
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

export function DbBrowserTab() {
  const [selected, setSelected] = useState<TableInfo | null>(null);
  return (
    <div className="bg-card border border-border rounded-lg p-4">
      {selected ? (
        <TableViewer info={selected} onBack={() => setSelected(null)} />
      ) : (
        <TableCatalog onPick={setSelected} />
      )}
    </div>
  );
}
