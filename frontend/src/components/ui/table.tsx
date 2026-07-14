import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Table primitives + `DataTable` — the shared replacement for the 28
 * hand-rolled `<table>`s across stock / admin / discussion / pages.
 *
 * Layer 1 (Table/TableHeader/...) are unopinionated shadcn-style
 * primitives for bespoke layouts. Layer 2 (`DataTable`) is the
 * column-driven component most call sites should use:
 *
 *   - density-aware rows via the `--row-h` token (Settings 密度 toggle)
 *   - numeric columns right-aligned; pair with `<Num>` / `<DeltaText>`
 *     in `render` for tabular digits — never inline `toFixed`
 *   - built-in empty slot (pass `<EmptyState …/>`)
 *   - `mobileMode` decides the <sm layout (see prop doc). Rule of
 *     thumb from the redesign spec: ≤6 columns with a clear
 *     primary/secondary hierarchy → "cards"; >6 columns or data that
 *     must be compared column-wise (OHLCV, 法人買賣超) → "scroll"
 *     (horizontal scroll with sticky first column).
 */

const Table = React.forwardRef<HTMLTableElement, React.HTMLAttributes<HTMLTableElement>>(
  ({ className, ...props }, ref) => (
    <table ref={ref} className={cn("w-full caption-bottom text-xs", className)} {...props} />
  )
);
Table.displayName = "Table";

const TableHeader = React.forwardRef<HTMLTableSectionElement, React.HTMLAttributes<HTMLTableSectionElement>>(
  ({ className, ...props }, ref) => (
    <thead ref={ref} className={cn("[&_tr]:border-b [&_tr]:border-border", className)} {...props} />
  )
);
TableHeader.displayName = "TableHeader";

const TableBody = React.forwardRef<HTMLTableSectionElement, React.HTMLAttributes<HTMLTableSectionElement>>(
  ({ className, ...props }, ref) => (
    <tbody ref={ref} className={cn("[&_tr:last-child]:border-0", className)} {...props} />
  )
);
TableBody.displayName = "TableBody";

const TableRow = React.forwardRef<HTMLTableRowElement, React.HTMLAttributes<HTMLTableRowElement>>(
  ({ className, ...props }, ref) => (
    <tr
      ref={ref}
      className={cn(
        "h-[var(--row-h)] border-b border-border/30 transition-colors hover:bg-accent/5",
        className
      )}
      {...props}
    />
  )
);
TableRow.displayName = "TableRow";

const TableHead = React.forwardRef<HTMLTableCellElement, React.ThHTMLAttributes<HTMLTableCellElement>>(
  ({ className, ...props }, ref) => (
    <th
      ref={ref}
      className={cn("px-2 py-2 text-left align-middle font-medium text-muted-foreground", className)}
      {...props}
    />
  )
);
TableHead.displayName = "TableHead";

const TableCell = React.forwardRef<HTMLTableCellElement, React.TdHTMLAttributes<HTMLTableCellElement>>(
  ({ className, ...props }, ref) => (
    <td ref={ref} className={cn("px-2 py-1.5 align-middle", className)} {...props} />
  )
);
TableCell.displayName = "TableCell";

// ── DataTable ────────────────────────────────────────────────────

export interface DataTableColumn<T> {
  /** Stable column id; used as React key and (when `render` is
   * omitted) as the property name looked up on the row object. */
  key: string;
  header: React.ReactNode;
  /** Cell renderer. Defaults to `String(row[key] ?? "—")`. Use
   * `<Num>` / `<DeltaText>` here for numeric cells. */
  render?: (row: T) => React.ReactNode;
  /** Right-aligns header + cells and applies tabular digits. */
  numeric?: boolean;
  align?: "left" | "right" | "center";
  headerClassName?: string;
  cellClassName?: string;
  /**
   * Role in `mobileMode="cards"`:
   *  - "primary": headline slot of the card (big text)
   *  - "secondary" (default): shown in the card's meta grid
   *  - "hidden": omitted on mobile cards
   */
  mobile?: "primary" | "secondary" | "hidden";
}

export interface DataTableProps<T> {
  columns: DataTableColumn<T>[];
  rows: T[];
  rowKey: (row: T, index: number) => string | number;
  /**
   * Layout below the `sm` breakpoint:
   *  - "scroll" (default): horizontal scroll, sticky first column
   *  - "cards": stacked cards driven by each column's `mobile` role
   */
  mobileMode?: "scroll" | "cards";
  /** Sticky `<thead>` for tall scrollable containers. The parent must
   * own the scroll (overflow-y) for stickiness to engage. */
  stickyHeader?: boolean;
  /** Rendered when `rows` is empty — pass `<EmptyState …/>`. */
  empty?: React.ReactNode;
  onRowClick?: (row: T) => void;
  className?: string;
  "aria-label"?: string;
  /** Screen-reader table caption (visually hidden). Give this when the
   * table has no visible heading so assistive tech can name it. */
  caption?: string;
  /** Marks the table `aria-busy` while data is (re)loading. */
  loading?: boolean;
  /** Wraps the row region in `aria-live="polite"` so screen readers
   * announce updates — use for tables whose values tick (quotes,
   * watchlist). Off by default (most tables are static). */
  live?: boolean;
}

function cellAlign<T>(col: DataTableColumn<T>): string {
  const align = col.align ?? (col.numeric ? "right" : "left");
  return align === "right" ? "text-right" : align === "center" ? "text-center" : "text-left";
}

function defaultCell<T>(row: T, col: DataTableColumn<T>): React.ReactNode {
  const v = (row as Record<string, unknown>)[col.key];
  return v === null || v === undefined || v === "" ? "—" : String(v);
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  mobileMode = "scroll",
  stickyHeader = false,
  empty,
  onRowClick,
  className,
  "aria-label": ariaLabel,
  caption,
  loading,
  live,
}: DataTableProps<T>) {
  if (!rows.length) {
    return (
      <>{empty ?? <p className="p-6 text-center text-sm text-muted-foreground">—</p>}</>
    );
  }

  const table = (
    <Table aria-label={ariaLabel} aria-busy={loading || undefined}>
      {caption && <caption className="sr-only">{caption}</caption>}
      <TableHeader className={stickyHeader ? "sticky top-0 z-10 bg-card" : undefined}>
        <tr className="border-b border-border">
          {columns.map((col, i) => (
            <TableHead
              key={col.key}
              scope="col"
              className={cn(
                cellAlign(col),
                mobileMode === "scroll" && i === 0 && "sticky left-0 bg-card sm:static sm:bg-transparent",
                col.headerClassName
              )}
            >
              {col.header}
            </TableHead>
          ))}
        </tr>
      </TableHeader>
      <TableBody aria-live={live ? "polite" : undefined}>
        {rows.map((row, i) => (
          <TableRow
            key={rowKey(row, i)}
            onClick={onRowClick ? () => onRowClick(row) : undefined}
            className={onRowClick ? "cursor-pointer" : undefined}
          >
            {columns.map((col, i) => (
              <TableCell
                key={col.key}
                className={cn(
                  cellAlign(col),
                  col.numeric && "font-mono tabular-nums",
                  mobileMode === "scroll" && i === 0 && "sticky left-0 bg-card sm:static sm:bg-transparent",
                  col.cellClassName
                )}
              >
                {col.render ? col.render(row) : defaultCell(row, col)}
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );

  if (mobileMode === "cards") {
    const primary = columns.filter((c) => c.mobile === "primary");
    const secondary = columns.filter((c) => (c.mobile ?? "secondary") === "secondary");
    return (
      <div className={className}>
        <div className="hidden sm:block overflow-x-auto">{table}</div>
        <ul
          className="sm:hidden space-y-2"
          aria-label={ariaLabel}
          aria-live={live ? "polite" : undefined}
          aria-busy={loading || undefined}
        >
          {rows.map((row, i) => (
            <li
              key={rowKey(row, i)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={cn(
                "rounded-md border border-border bg-surface-1 p-3 shadow-highlight",
                onRowClick && "cursor-pointer active:bg-accent/10"
              )}
            >
              {primary.length > 0 && (
                <div className="mb-1.5 flex items-baseline justify-between gap-2 text-sm font-medium">
                  {primary.map((col) => (
                    <span key={col.key} className={cn(col.numeric && "font-mono tabular-nums")}>
                      {col.render ? col.render(row) : defaultCell(row, col)}
                    </span>
                  ))}
                </div>
              )}
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                {secondary.map((col) => (
                  <div key={col.key} className="flex items-baseline justify-between gap-2">
                    <dt className="text-muted-foreground">{col.header}</dt>
                    <dd className={cn(col.numeric && "font-mono tabular-nums")}>
                      {col.render ? col.render(row) : defaultCell(row, col)}
                    </dd>
                  </div>
                ))}
              </dl>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  return <div className={cn("overflow-x-auto", className)}>{table}</div>;
}

export { Table, TableHeader, TableBody, TableRow, TableHead, TableCell };
