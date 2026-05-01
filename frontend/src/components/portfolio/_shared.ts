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
