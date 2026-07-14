import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ChartTooltip } from "@/components/ui/ChartTooltip";
import { Loading } from "./_atoms";
import { fetchDividends } from "./_shared";
import { DataTable, type DataTableColumn } from "../ui/table";

export function DividendsPanel({ symbol, currentPrice }: { symbol: string; currentPrice: number | null }) {
  const { t } = useTranslation();
  const { data = [], isLoading } = useQuery({
    queryKey: ["dividends", symbol],
    queryFn: () => fetchDividends(symbol),
    staleTime: 6 * 3_600_000,
    gcTime: 24 * 3_600_000, // dividends tier — annual data, cache for a day
  });

  if (isLoading) return <Loading />;
  if (!data.length) {
    return <div className="p-6 text-muted-foreground text-sm">{t("stock.dividends.no_data")}</div>;
  }

  // Yearly aggregate. FinMind dates are typically YYYY-MM-DD; if year is
  // ambiguous we treat the date prefix as the year bucket.
  const byYear: Record<string, { cash: number; stock: number }> = {};
  for (const r of data) {
    const yr = (r.date || "").slice(0, 4);
    if (!yr) continue;
    if (!byYear[yr]) byYear[yr] = { cash: 0, stock: 0 };
    byYear[yr].cash += r.cash_dividend;
    byYear[yr].stock += r.stock_dividend;
  }
  const years = Object.keys(byYear).sort();
  const chartData = years.map((y) => ({
    year: y,
    cash: Number(byYear[y].cash.toFixed(4)),
    stock: Number(byYear[y].stock.toFixed(4)),
  }));

  const last5 = chartData.slice(-5);
  const avgCash5 = last5.length
    ? last5.reduce((s, r) => s + r.cash, 0) / last5.length
    : 0;
  const ttmYield = currentPrice && currentPrice > 0
    ? (avgCash5 / currentPrice) * 100
    : null;
  const lastPayout = data[data.length - 1];

  type DivRow = (typeof data)[number];
  const rows = [...data].reverse().slice(0, 30);
  const columns: DataTableColumn<DivRow>[] = [
    {
      key: "date",
      header: t("stock.dividends.announce_date"),
      mobile: "primary",
      render: (r) => <span className="text-muted-foreground">{r.date}</span>,
    },
    {
      key: "ex_date",
      header: t("stock.dividends.ex_date"),
      render: (r) => <span className="text-muted-foreground">{r.ex_date ?? "—"}</span>,
    },
    {
      key: "cash",
      header: `${t("stock.dividends.cash")} (TWD)`,
      numeric: true,
      // Cash payout — a positive amount, so the semantic `success` token
      // (theme-following) rather than a hardcoded green that bypassed the
      // value layer + market-colour convention.
      render: (r) => (
        <span className="num font-medium text-success">
          {r.cash_dividend ? r.cash_dividend.toFixed(3) : "—"}
        </span>
      ),
    },
    {
      key: "stock",
      header: `${t("stock.dividends.stock")} (${t("stock.share_unit")})`,
      numeric: true,
      render: (r) => (
        <span className="num">{r.stock_dividend ? r.stock_dividend.toFixed(3) : "—"}</span>
      ),
    },
  ];

  return (
    <div className="p-4 space-y-4">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.dividends.last_payout")}</div>
          <div className="text-lg font-semibold text-foreground">
            {lastPayout ? lastPayout.cash_dividend.toFixed(3) : "—"}
          </div>
          {lastPayout?.ex_date && (
            <div className="text-[10px] text-muted-foreground mt-0.5">{t("stock.dividends.ex_date")}: {lastPayout.ex_date}</div>
          )}
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.dividends.avg_5y")}</div>
          <div className="text-lg font-semibold text-foreground">{avgCash5.toFixed(3)}</div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.dividends.implied_yield")}</div>
          <div className="text-lg font-semibold text-foreground">
            {ttmYield == null ? "—" : `${ttmYield.toFixed(2)}%`}
          </div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.dividends.years_paid")}</div>
          <div className="text-lg font-semibold text-foreground">{years.length}</div>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={240}>
        <BarChart data={chartData} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <XAxis dataKey="year" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} />
          <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} width={40} />
          <Tooltip content={<ChartTooltip valueFormatter={(v) => v.toFixed(3)} />} />
          <Legend wrapperStyle={{ fontSize: 11 }} iconSize={8} />
          <Bar dataKey="cash" name={t("stock.dividends.cash")} fill="hsl(var(--chart-4))" radius={[2, 2, 0, 0]} />
          <Bar dataKey="stock" name={t("stock.dividends.stock")} fill="hsl(var(--chart-2))" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>

      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r, i) => `${r.date}-${i}`}
        mobileMode="cards"
        aria-label={t("stock.dividends.tab")}
      />
    </div>
  );
}
