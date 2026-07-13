import type { UseQueryResult } from "@tanstack/react-query";

import { DataTable, type DataTableColumn } from "../../ui/table";
import type { ApiKeyItem } from "./types";

/**
 * The keys area: loading placeholder, empty-state hint, and the keys
 * DataTable. Pure display — the keys query + column defs live in the
 * entry card. The three original sibling `{keysQuery… && …}` guards
 * are preserved verbatim inside a fragment.
 */
export function KeysTable({
  keysQuery,
  keyColumns,
}: {
  keysQuery: UseQueryResult<ApiKeyItem[], Error>;
  keyColumns: DataTableColumn<ApiKeyItem>[];
}) {
  return (
    <>
      {keysQuery.isLoading && (
        <div className="text-sm text-muted-foreground">Loading…</div>
      )}
      {keysQuery.data && keysQuery.data.length === 0 && (
        <div className="rounded border border-border bg-muted/30 p-6 text-center text-sm text-muted-foreground">
          No API keys yet. Issue one above to give a customer access
          to <code className="rounded bg-muted px-1">/api/finmind/data/...</code>
          .
        </div>
      )}
      {keysQuery.data && keysQuery.data.length > 0 && (
        <DataTable
          columns={keyColumns}
          rows={keysQuery.data}
          rowKey={(k) => k.id}
          mobileMode="scroll"
          aria-label="API keys"
        />
      )}
    </>
  );
}
