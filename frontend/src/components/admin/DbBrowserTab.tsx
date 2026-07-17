import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  Database,
  Table2,
  Lock,
  ArrowLeft,
  ArrowUp,
  ArrowDown,
  Search,
  Download,
  X,
} from "lucide-react";
import api from "@/lib/api";
import { DataTable, type DataTableColumn } from "@/components/ui/table";
import { EmptyState } from "@/components/ui/EmptyState";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Num } from "@/components/Num";

/**
 * Read-only database browser (AdminPage「DB」tab). Left: the table
 * catalog with row-estimate + size and a name search. Click a table →
 * paginated row view with header-click sorting, a single-column
 * filter, cell expansion and CSV export. All heavy lifting + safety
 * live in the backend `/api/admin/db/*` endpoints; this is a thin
 * viewer.
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

type CellValue = string | number | boolean | null;

interface RowsPage {
  schema_name: string;
  table: string;
  columns: ColumnInfo[];
  rows: CellValue[][];
  page: number;
  page_size: number;
  total: number | null;
  latest_at: string | null;
}

const FILTER_OPS = ["eq", "ilike", "gte", "lte"] as const;
type FilterOp = (typeof FILTER_OPS)[number];

interface Filter {
  col: string;
  op: FilterOp;
  val: string;
}

const PAGE_SIZES = [50, 100, 200, 500];

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

function displayName(t: TableInfo): string {
  return t.schema_name === "public" ? t.table : `${t.schema_name}.${t.table}`;
}

/** Pretty-print JSON-looking cell values in the expand dialog. */
function prettyCell(value: CellValue): string {
  const s = String(value);
  const trimmed = s.trim();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2);
    } catch {
      return s;
    }
  }
  return s;
}

function TableCatalog({ onPick }: { onPick: (t: TableInfo) => void }) {
  const { t } = useTranslation();
  const [search, setSearch] = useState("");
  const { data = [], isLoading } = useQuery<TableInfo[]>({
    queryKey: ["db-tables"],
    queryFn: () => api.get("/admin/db/tables").then((r) => r.data),
  });

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return data;
    return data.filter((r) => displayName(r).toLowerCase().includes(q));
  }, [data, search]);

  const columns: DataTableColumn<TableInfo>[] = [
    {
      key: "table",
      header: t("dbBrowser.table"),
      render: (r) => (
        <span className="inline-flex items-center gap-1.5 font-medium">
          <Table2 className="h-3.5 w-3.5 text-muted-foreground/60" aria-hidden="true" />
          {displayName(r)}
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
    <div className="space-y-3">
      <label className="relative block max-w-xs">
        <Search
          className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground/60"
          aria-hidden="true"
        />
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t("dbBrowser.searchTables")}
          aria-label={t("dbBrowser.searchTables")}
          className="w-full rounded border border-border bg-background py-1.5 pl-7 pr-2 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
        />
      </label>
      <DataTable
        aria-label={t("dbBrowser.catalog")}
        columns={columns}
        rows={filtered}
        rowKey={(r) => `${r.schema_name}.${r.table}`}
        mobileMode="cards"
        onRowClick={onPick}
        empty={<EmptyState icon={Database} title={t("dbBrowser.noTables")} />}
      />
    </div>
  );
}

function TableViewer({ info, onBack }: { info: TableInfo; onBack: () => void }) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [sort, setSort] = useState<{ col: string; dir: "asc" | "desc" } | null>(null);
  const [filter, setFilter] = useState<Filter | null>(null);
  const [draft, setDraft] = useState<Filter>({ col: "", op: "ilike", val: "" });
  const [expanded, setExpanded] = useState<{ col: string; value: CellValue } | null>(null);
  const [exporting, setExporting] = useState(false);

  const queryParams = useMemo(() => {
    const params: Record<string, string | number> = { page, page_size: pageSize };
    if (sort) {
      params.order_by = sort.col;
      params.order_dir = sort.dir;
    }
    if (filter) {
      params.filter_col = filter.col;
      params.filter_op = filter.op;
      params.filter_val = filter.val;
    }
    return params;
  }, [page, pageSize, sort, filter]);

  const { data, isLoading, isError } = useQuery<RowsPage>({
    queryKey: ["db-rows", info.schema_name, info.table, queryParams],
    queryFn: () =>
      api
        .get(`/admin/db/tables/${info.schema_name}/${info.table}/rows`, {
          params: queryParams,
        })
        .then((r) => r.data),
    placeholderData: (prev) => prev,
  });

  const opLabels: Record<FilterOp, string> = {
    eq: t("dbBrowser.opEq"),
    ilike: t("dbBrowser.opContains"),
    gte: "≥",
    lte: "≤",
  };

  const cycleSort = (col: string) => {
    setPage(1);
    setSort((prev) => {
      if (prev?.col !== col) return { col, dir: "desc" };
      if (prev.dir === "desc") return { col, dir: "asc" };
      return null;
    });
  };

  const applyFilter = () => {
    if (!draft.col || draft.val === "") return;
    setPage(1);
    setFilter({ ...draft });
  };

  const clearFilter = () => {
    setPage(1);
    setFilter(null);
    setDraft((d) => ({ ...d, val: "" }));
  };

  const exportCsv = async () => {
    setExporting(true);
    try {
      const { page: _p, page_size: _ps, ...exportParams } = queryParams;
      const res = await api.get(
        `/admin/db/tables/${info.schema_name}/${info.table}/export.csv`,
        { params: exportParams, responseType: "blob" },
      );
      const url = URL.createObjectURL(res.data as Blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${info.schema_name}.${info.table}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
  };

  const columns = useMemo<DataTableColumn<CellValue[]>[]>(() => {
    if (!data) return [];
    return data.columns.map((col, i) => ({
      key: col.name,
      header: (
        <button
          type="button"
          onClick={() => cycleSort(col.name)}
          className="inline-flex items-center gap-1 hover:text-foreground"
          aria-label={`${t("dbBrowser.sortBy")} ${col.name}`}
        >
          {col.name}
          {col.masked && <Lock className="h-3 w-3 text-muted-foreground/50" aria-hidden="true" />}
          {sort?.col === col.name &&
            (sort.dir === "desc" ? (
              <ArrowDown className="h-3 w-3" aria-hidden="true" />
            ) : (
              <ArrowUp className="h-3 w-3" aria-hidden="true" />
            ))}
        </button>
      ),
      render: (row) => {
        const v = row[i];
        if (v === null) return <span className="text-muted-foreground/50">null</span>;
        return (
          <button
            type="button"
            onClick={() => setExpanded({ col: col.name, value: v })}
            className="block max-w-full cursor-pointer truncate text-left hover:text-foreground"
            title={t("dbBrowser.expandCell")}
          >
            {String(v)}
          </button>
        );
      },
      cellClassName: "font-mono text-meta max-w-[240px]",
    }));
  }, [data, sort, t]);

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
          {displayName(info)}
          {data?.latest_at && (
            <span className="ml-2 text-muted-foreground/60">· {t("dbBrowser.latest")} {data.latest_at}</span>
          )}
        </span>
      </div>

      {data && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <select
            value={draft.col}
            onChange={(e) => setDraft((d) => ({ ...d, col: e.target.value }))}
            aria-label={t("dbBrowser.filterField")}
            className="rounded border border-border bg-background px-2 py-1"
          >
            <option value="">{t("dbBrowser.filterField")}</option>
            {data.columns.map((c) => (
              <option key={c.name} value={c.name}>
                {c.name}
              </option>
            ))}
          </select>
          <select
            value={draft.op}
            onChange={(e) => setDraft((d) => ({ ...d, op: e.target.value as FilterOp }))}
            aria-label={t("dbBrowser.filterOp")}
            className="rounded border border-border bg-background px-2 py-1"
          >
            {FILTER_OPS.map((op) => (
              <option key={op} value={op}>
                {opLabels[op]}
              </option>
            ))}
          </select>
          <input
            value={draft.val}
            onChange={(e) => setDraft((d) => ({ ...d, val: e.target.value }))}
            onKeyDown={(e) => e.key === "Enter" && applyFilter()}
            placeholder={t("dbBrowser.filterValue")}
            aria-label={t("dbBrowser.filterValue")}
            className="w-36 rounded border border-border bg-background px-2 py-1"
          />
          <button
            onClick={applyFilter}
            disabled={!draft.col || draft.val === ""}
            className="rounded border border-border px-2 py-1 hover:text-foreground disabled:opacity-40"
          >
            {t("dbBrowser.apply")}
          </button>
          {filter && (
            <button
              onClick={clearFilter}
              className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-muted-foreground hover:text-foreground"
            >
              <X className="h-3 w-3" aria-hidden="true" />
              {t("dbBrowser.clear")}
            </button>
          )}
          <span className="flex-1" />
          <button
            onClick={exportCsv}
            disabled={exporting}
            className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 hover:text-foreground disabled:opacity-40"
          >
            <Download className="h-3 w-3" aria-hidden="true" />
            {t("dbBrowser.exportCsv")}
          </button>
        </div>
      )}

      {isError ? (
        <p className="text-xs text-destructive">{t("common.error")}</p>
      ) : isLoading || !data ? (
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
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>
              {t("dbBrowser.total")}: <Num value={total} format="compact" className="text-foreground" />
            </span>
            <div className="flex items-center gap-2">
              <select
                value={pageSize}
                onChange={(e) => {
                  setPageSize(Number(e.target.value));
                  setPage(1);
                }}
                aria-label={t("dbBrowser.perPage")}
                className="rounded border border-border bg-background px-1.5 py-1"
              >
                {PAGE_SIZES.map((s) => (
                  <option key={s} value={s}>
                    {s} {t("dbBrowser.perPage")}
                  </option>
                ))}
              </select>
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                className="rounded border border-border px-2 py-1 disabled:opacity-40 hover:text-foreground"
              >
                {t("common.prev")}
              </button>
              <span className="tabular-nums">
                <input
                  type="number"
                  min={1}
                  max={pageCount}
                  value={page}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    if (Number.isFinite(v) && v >= 1 && v <= pageCount) setPage(v);
                  }}
                  aria-label={t("dbBrowser.page")}
                  className="w-14 rounded border border-border bg-background px-1 py-0.5 text-center"
                />{" "}
                / {pageCount}
              </span>
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

      <Dialog open={expanded !== null} onOpenChange={(open) => !open && setExpanded(null)}>
        <DialogContent className="max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="font-mono text-sm">{expanded?.col}</DialogTitle>
          </DialogHeader>
          <pre className="whitespace-pre-wrap break-all font-mono text-xs text-foreground">
            {expanded ? prettyCell(expanded.value) : ""}
          </pre>
        </DialogContent>
      </Dialog>
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
