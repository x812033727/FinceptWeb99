import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Loading } from "./_atoms";
import { fetchFinancials } from "./_shared";

export function FinancialsPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const { data, isLoading } = useQuery({
    queryKey: ["financials", "US", symbol],
    queryFn: () => fetchFinancials(symbol),
    staleTime: 3_600_000,
    gcTime: 24 * 3_600_000, // fundamentals tier — keep cached across tab/page hops for a day
  });

  if (isLoading) return <Loading />;
  if (!data) return <div className="p-6 text-muted-foreground text-sm">{t("common.no_data")}</div>;

  // yfinance returns {income_statement, balance_sheet, cash_flow} as record of records
  const raw = data.data as Record<string, Record<string, number | null>> | null;
  if (!raw) return <div className="p-6 text-muted-foreground text-sm">{t("common.no_data")}</div>;

  const sections: Array<{ title: string; key: string }> = [
    { title: t("stock.income_statement"), key: "income_statement" },
    { title: t("stock.balance_sheet"), key: "balance_sheet" },
    { title: t("stock.cash_flow"), key: "cash_flow" },
  ];

  return (
    <div className="space-y-6 p-4">
      {sections.map(({ title, key }) => {
        const table = raw[key] as unknown as Record<string, Record<string, number | null>> | undefined;
        if (!table || !Object.keys(table).length) return null;

        // Rows = metric names, Columns = dates
        const rows = Object.keys(table);
        const cols = rows.length
          ? Object.keys(table[rows[0]] ?? {}).sort().reverse().slice(0, 5)
          : [];

        if (!cols.length) return null;

        return (
          <div key={key}>
            <h3 className="text-sm font-semibold text-foreground mb-2">{title}</h3>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-muted-foreground">
                    <th className="text-left py-1.5 pr-4 font-medium min-w-[200px]">Metric</th>
                    {cols.map((c) => (
                      <th key={c} className="text-right py-1.5 px-2 font-medium">{c.slice(0, 4)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row} className="border-b border-border/30 hover:bg-accent/5">
                      <td className="py-1.5 pr-4 text-muted-foreground">{row}</td>
                      {cols.map((c) => {
                        const v = (table[row] as Record<string, number | null>)?.[c];
                        return (
                          <td key={c} className="text-right py-1.5 px-2 text-foreground">
                            {v == null ? "—" : Math.abs(v) >= 1e9
                              ? `${(v / 1e9).toFixed(2)}B`
                              : Math.abs(v) >= 1e6
                              ? `${(v / 1e6).toFixed(1)}M`
                              : v.toLocaleString()}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}
    </div>
  );
}
