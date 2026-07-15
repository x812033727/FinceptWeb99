/**
 * Shared helpers + types for the PortfolioPage extraction.
 *
 * Pulled out so the modal forms (AddTransaction / EditTransaction) can
 * share the FX rate auto-fill logic and TransactionHistory + the edit
 * modal can agree on the row shape without redeclaring it.
 */
import api from "@/lib/api";

/**
 * When the user picks a foreign-currency stock and a transaction date,
 * the form auto-fills the FX field with the rate FRED reported on that
 * trade day so cost basis is denominated correctly. The user can still
 * override by editing the field; once they do, `userPinnedFx` flips to
 * true and we stop overwriting their value.
 */
export async function fetchSuggestedFxRate(
  portfolioId: string,
  market: string,
  txDate: string,
): Promise<number | null> {
  try {
    const res = await api.get<{ fx_rate: number }>(
      `/portfolio/${portfolioId}/fx-rate?market=${market}&tx_date=${txDate}`,
    );
    return res.data.fx_rate;
  } catch {
    return null;
  }
}

/**
 * Diverging heatmap cell fill for the risk dashboard's correlation
 * matrix: −1 → hsl(var(--down)), 0 → transparent (neutral midpoint
 * stays on the surface), +1 → hsl(var(--up)). Alpha scales with |corr|
 * and caps at 0.85 so cell text stays readable.
 */
export function correlationCellStyle(corr: number): { backgroundColor: string } {
  const clamped = Math.max(-1, Math.min(1, corr));
  const alpha = Math.min(1, Math.abs(clamped)) * 0.85;
  const token = clamped >= 0 ? "--up" : "--down";
  return { backgroundColor: `hsl(var(${token}) / ${alpha.toFixed(3)})` };
}

export interface TransactionRow {
  id: string;
  symbol: string;
  market: string;
  tx_type: string;
  quantity: number;
  price: number;
  fx_rate: number;
  tx_date: string;
  notes: string | null;
  created_at: string;
}

export function exportCSV(rows: Record<string, unknown>[], filename: string) {
  if (!rows.length) return;
  const headers = Object.keys(rows[0]);
  const csv = [
    headers.join(","),
    ...rows.map((r) =>
      headers
        .map((h) => {
          const v = r[h];
          const s = String(v ?? "");
          return s.includes(",") || s.includes('"') ? `"${s.replace(/"/g, '""')}"` : s;
        })
        .join(",")
    ),
  ].join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export interface ImportedTransactionRow {
  [key: string]: unknown;
  tx_date: string;
  symbol: string;
  market: string;
  tx_type: string;
  quantity: number;
  price: number;
  fx_rate?: number;
  notes?: string;
}

/** Parse RFC-4180-style CSV and map both exported and API column names. */
export function parseTransactionCSV(input: string): ImportedTransactionRow[] {
  const records: string[][] = [];
  let record: string[] = [];
  let field = "";
  let quoted = false;
  const text = input.replace(/^\uFEFF/, "");
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (char === '"') {
      if (quoted && text[i + 1] === '"') {
        field += '"';
        i += 1;
      } else {
        quoted = !quoted;
      }
    } else if (char === "," && !quoted) {
      record.push(field.trim());
      field = "";
    } else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && text[i + 1] === "\n") i += 1;
      record.push(field.trim());
      if (record.some(Boolean)) records.push(record);
      record = [];
      field = "";
    } else {
      field += char;
    }
  }
  record.push(field.trim());
  if (record.some(Boolean)) records.push(record);
  if (quoted) throw new Error("Unclosed quoted field");
  if (records.length < 2) throw new Error("CSV must contain a header and at least one row");

  const headers = records[0].map((header) => header.toLowerCase());
  const indexOf = (...aliases: string[]) => headers.findIndex((header) => aliases.includes(header));
  const indexes = {
    tx_date: indexOf("tx_date", "date"),
    symbol: indexOf("symbol", "ticker"),
    market: indexOf("market"),
    tx_type: indexOf("tx_type", "type", "side"),
    quantity: indexOf("quantity", "qty"),
    price: indexOf("price"),
    fx_rate: indexOf("fx_rate", "fx"),
    notes: indexOf("notes", "note"),
  };
  const missing = Object.entries(indexes)
    .filter(([key, index]) => index < 0 && !["fx_rate", "notes"].includes(key))
    .map(([key]) => key);
  if (missing.length) throw new Error(`Missing columns: ${missing.join(", ")}`);
  const value = (row: string[], index: number) => index < 0 ? "" : (row[index] ?? "");
  return records.slice(1).map((row) => {
    const fx = value(row, indexes.fx_rate);
    const notes = value(row, indexes.notes);
    return {
      tx_date: value(row, indexes.tx_date),
      symbol: value(row, indexes.symbol),
      market: value(row, indexes.market),
      tx_type: value(row, indexes.tx_type),
      quantity: Number(value(row, indexes.quantity)),
      price: Number(value(row, indexes.price)),
      ...(fx ? { fx_rate: Number(fx) } : {}),
      ...(notes ? { notes } : {}),
    };
  });
}
