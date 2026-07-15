import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import api from "@/lib/api";
import { useSymbolSearch, type SearchResult } from "@/hooks/useSymbolSearch";
import { ChartTooltip } from "@/components/ui/ChartTooltip";

type CompareSeries = {
  instrument: string;
  market: string;
  symbol: string;
  base_date: string;
  end_date: string;
  observations: number;
  return_pct: number;
  max_drawdown_pct: number;
  annualised_volatility_pct: number | null;
  data_source: string | null;
  points: { date: string; value: number }[];
};

type CompareResponse = {
  period: string;
  common_base_date: string | null;
  currency_note: string;
  series: CompareSeries[];
  excluded: { market: string; symbol: string; reason: string }[];
};

const COLORS = ["#22c55e", "#38bdf8", "#f59e0b", "#e879f9", "#f43f5e"];
const VALID = /^(TW|US|CRYPTO):[A-Z0-9.\-]{1,20}$/;
const PERIODS = ["1m", "3m", "6m", "1y"] as const;

function initialSymbols(raw: string | null): string[] {
  const values = (raw ?? "").split(",").map((value) => value.trim().toUpperCase()).filter((value) => VALID.test(value));
  return [...new Set(values)].slice(0, 5);
}

function initialPeriod(raw: string | null): string {
  return PERIODS.includes(raw as (typeof PERIODS)[number]) ? raw as string : "3m";
}

function signed(value: number | null) {
  return value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

export default function ComparisonPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [selected, setSelected] = useState<string[]>(() => initialSymbols(searchParams.get("symbols")));
  const [period, setPeriod] = useState(() => initialPeriod(searchParams.get("period")));
  const [search, setSearch] = useState("");
  const { results, loading: searching } = useSymbolSearch(search, { maxResults: 8 });

  const instruments = selected.join(",");
  const query = useQuery<CompareResponse>({
    queryKey: ["market-comparison", instruments, period],
    queryFn: () => api.get(`/global/compare-history?instruments=${encodeURIComponent(instruments)}&period=${period}`).then((response) => response.data),
    enabled: selected.length >= 2,
    staleTime: 300_000,
  });

  function add(result: SearchResult) {
    const id = `${result.market}:${result.symbol}`;
    if (selected.length >= 5 || selected.includes(id)) return;
    const next = [...selected, id];
    setSelected(next);
    setSearch("");
    setSearchParams({ symbols: next.join(","), period });
  }

  function remove(id: string) {
    const next = selected.filter((value) => value !== id);
    setSelected(next);
    setSearchParams(next.length ? { symbols: next.join(","), period } : {});
  }

  function changePeriod(next: string) {
    setPeriod(next);
    if (selected.length) setSearchParams({ symbols: selected.join(","), period: next });
  }

  const chartData = useMemo(() => {
    const rows = new Map<string, Record<string, string | number>>();
    for (const series of query.data?.series ?? []) {
      for (const point of series.points) {
        const row = rows.get(point.date) ?? { date: point.date };
        row[series.instrument] = point.value;
        rows.set(point.date, row);
      }
    }
    return [...rows.values()].sort((a, b) => String(a.date).localeCompare(String(b.date)));
  }, [query.data]);

  return (
    <div className="space-y-section p-gutter sm:p-page">
      <div>
        <h1 className="text-title font-bold">{t("comparison.title")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{t("comparison.subtitle")}</p>
      </div>

      <div className="rounded-lg border border-border bg-card p-4 shadow-highlight">
        <div className="flex flex-wrap gap-2">
          {selected.map((id, index) => (
            <button key={id} onClick={() => remove(id)} className="rounded-full border border-border px-3 py-1 text-xs" style={{ borderColor: COLORS[index] }} title={t("comparison.remove")}>
              {id} <span className="ml-1 text-muted-foreground">×</span>
            </button>
          ))}
        </div>
        {selected.length < 5 && (
          <div className="relative mt-3 max-w-md">
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t("comparison.search")} aria-label={t("comparison.search")} className="w-full rounded border border-border bg-background px-3 py-2 text-sm" />
            {search.trim() && (
              <div className="absolute z-20 mt-1 max-h-56 w-full overflow-auto rounded border border-border bg-popover shadow-lg">
                {searching && <p className="p-3 text-xs text-muted-foreground">{t("common.loading")}</p>}
                {!searching && results.filter((row) => !selected.includes(`${row.market}:${row.symbol}`)).map((row) => (
                  <button key={`${row.market}:${row.symbol}`} onClick={() => add(row)} className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-accent/10">
                    <span>{row.symbol}</span><span className="text-xs text-muted-foreground">{row.market}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        <p className="mt-2 text-xs text-muted-foreground">{t("comparison.pick_hint")}</p>
      </div>

      <div className="flex gap-1">
        {PERIODS.map((value) => <button key={value} onClick={() => changePeriod(value)} className={`rounded px-3 py-1.5 text-xs ${period === value ? "bg-primary text-primary-foreground" : "bg-secondary/40 text-muted-foreground"}`}>{value.toUpperCase()}</button>)}
      </div>

      {selected.length < 2 ? (
        <div className="rounded-lg border border-dashed border-border p-14 text-center text-sm text-muted-foreground">{t("comparison.need_two")}</div>
      ) : query.isLoading ? (
        <div className="h-80 animate-pulse rounded-lg bg-secondary/20" />
      ) : query.isError ? (
        <div className="rounded-lg border border-negative/30 p-8 text-sm text-negative">{t("comparison.error")}</div>
      ) : (
        <>
          <div className="rounded-lg border border-border bg-card p-4">
            <ResponsiveContainer width="100%" height={360}>
              <LineChart data={chartData} margin={{ top: 10, right: 10, bottom: 5, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                <XAxis dataKey="date" tick={{ fontSize: 10 }} tickFormatter={(value) => String(value).slice(5)} minTickGap={35} />
                <YAxis tick={{ fontSize: 10 }} domain={["auto", "auto"]} tickFormatter={(value) => Number(value).toFixed(0)} />
                <Tooltip content={<ChartTooltip valueFormatter={(value) => `${value.toFixed(2)}`} />} />
                <Legend />
                {(query.data?.series ?? []).map((series, index) => <Line key={series.instrument} type="monotone" dataKey={series.instrument} stroke={COLORS[index]} strokeWidth={2} dot={false} connectNulls />)}
              </LineChart>
            </ResponsiveContainer>
          </div>

          <div className="overflow-x-auto rounded-lg border border-border bg-card">
            <table className="w-full min-w-[650px] text-sm"><thead className="bg-secondary/20 text-left text-xs text-muted-foreground"><tr><th className="p-3">{t("comparison.instrument")}</th><th className="p-3">{t("comparison.return")}</th><th className="p-3">{t("comparison.drawdown")}</th><th className="p-3">{t("comparison.volatility")}</th><th className="p-3">{t("comparison.observations")}</th><th className="p-3">{t("comparison.source")}</th></tr></thead><tbody>
              {(query.data?.series ?? []).map((series) => <tr key={series.instrument} className="border-t border-border"><td className="p-3 font-medium">{series.instrument}</td><td className={`p-3 ${series.return_pct >= 0 ? "text-positive" : "text-negative"}`}>{signed(series.return_pct)}</td><td className="p-3 text-negative">{signed(series.max_drawdown_pct)}</td><td className="p-3">{signed(series.annualised_volatility_pct)}</td><td className="p-3">{series.observations}</td><td className="p-3 text-xs text-muted-foreground">{series.data_source ?? "—"}</td></tr>)}
            </tbody></table>
          </div>
          {!!query.data?.excluded.length && <p className="text-xs text-warning">{t("comparison.excluded", { symbols: query.data.excluded.map((row) => `${row.market}:${row.symbol}`).join(", ") })}</p>}
          <p className="text-xs text-muted-foreground">{t("comparison.currency_note")}</p>
        </>
      )}
    </div>
  );
}
