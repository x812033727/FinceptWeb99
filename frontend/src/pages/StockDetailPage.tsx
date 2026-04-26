import { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart, Bar, LineChart, Line, PieChart, Pie, XAxis, YAxis, Tooltip,
  ResponsiveContainer, ReferenceLine, Cell, Legend,
} from "recharts";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import CandlestickChart from "@/components/charts/CandlestickChart";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { OHLCVBar, Market } from "@/types/market";

// ── types ──────────────────────────────────────────────────────────

type Period = "1d" | "5d" | "1mo" | "3mo" | "1y" | "5y";
type Interval = "1m" | "5m" | "15m" | "1h" | "1d" | "1wk";

type USTab = "chart" | "financials" | "options" | "news";
type TWTab = "chart" | "health" | "valuation" | "holdings" | "dividends" | "institutional" | "margin" | "revenue" | "news";
type CryptoTab = "chart" | "news";

const isTWETF = (symbol: string) => /^00\d{2,4}[A-Z]?$/.test(symbol);

interface OptionRow {
  strike: number;
  expiration_date?: string;
  contract_type: string;
  bid?: number;
  ask?: number;
  last_price?: number;
  volume?: number;
  open_interest?: number;
  implied_volatility?: number;
  delta?: number;
  gamma?: number;
}

interface InstitutionalRow {
  date: string;
  fini_buy: number;
  fini_sell: number;
  sitc_buy: number;
  sitc_sell: number;
  dealer_buy: number;
  dealer_sell: number;
}

interface MarginRow {
  date: string;
  margin_purchase: number;
  margin_balance: number;
  short_sale: number;
  short_balance: number;
}

interface RevenueRow {
  date: string;
  revenue: number;
  revenue_mom: number | null;
  revenue_yoy: number | null;
}

type Light = "green" | "yellow" | "red" | "gray";

interface HealthPeriod {
  date: string;
  revenue: number | null;
  net_income: number | null;
  eps: number | null;
  gross_margin: number | null;
  operating_margin: number | null;
  net_margin: number | null;
  debt_ratio: number | null;
  current_ratio: number | null;
  operating_cf: number | null;
  free_cf: number | null;
  total_equity: number | null;
}

interface HealthResponse {
  symbol: string;
  market: "TW";
  periods: HealthPeriod[];
  summary: {
    latest_roe: number | null;
    latest_debt_ratio: number | null;
    latest_gross_margin: number | null;
    latest_net_margin: number | null;
    revenue_yoy: number | null;
    cf_positive_streak_4q: number;
  };
  lights: {
    profitability: Light;
    safety: Light;
    growth: Light;
    cash_flow: Light;
  };
}

interface ValuationBandResponse {
  symbol: string;
  metric: "pe" | "pb";
  series: { date: string; value: number | null }[];
  stats: {
    mean: number | null;
    std: number | null;
    min: number | null;
    max: number | null;
    p10: number | null;
    p25: number | null;
    p50: number | null;
    p75: number | null;
    p90: number | null;
    current: number | null;
    current_z: number | null;
  };
}

interface DividendRow {
  date: string;
  ex_date: string | null;
  cash_dividend: number;
  stock_dividend: number;
}

interface ETFHolding {
  symbol: string;
  name_zh: string;
  weight: number;
}

interface ETFHoldingsResponse {
  as_of: string | null;
  holdings: ETFHolding[];
}

// ── API helpers ────────────────────────────────────────────────────

const PERIOD_INTERVAL: Record<Period, Interval> = {
  "1d": "5m", "5d": "15m", "1mo": "1h", "3mo": "1d", "1y": "1d", "5y": "1wk",
};

// Crypto period → Kraken interval + bar limit. Kraken caps at 720 bars/req.
const CRYPTO_PERIOD: Record<Period, { interval: string; limit: number }> = {
  "1d": { interval: "5m", limit: 288 },
  "5d": { interval: "15m", limit: 480 },
  "1mo": { interval: "1h", limit: 720 },
  "3mo": { interval: "4h", limit: 540 },
  "1y": { interval: "1d", limit: 365 },
  "5y": { interval: "1w", limit: 260 },
};

const fetchHistory = (mkt: Market, sym: string, period: Period) =>
  api.get<OHLCVBar[]>(
    mkt === "US"
      ? `/us/history/${sym}?period=${period}&interval=${PERIOD_INTERVAL[period]}`
      : mkt === "CRYPTO"
        ? `/crypto/history/${sym}?interval=${CRYPTO_PERIOD[period].interval}&limit=${CRYPTO_PERIOD[period].limit}`
        : `/tw/history/${sym}?months=${period === "5y" ? 60 : period === "1y" ? 12 : 3}`
  ).then((r) => r.data);

const fetchQuote = (mkt: Market, sym: string) =>
  api.get<Record<string, unknown>>(
    mkt === "US" ? `/us/quote/${sym}`
      : mkt === "CRYPTO" ? `/crypto/quote/${sym}`
      : `/tw/quote/${sym}`
  ).then((r) => r.data);

const fetchFundamentals = (mkt: Market, sym: string) =>
  api.get<Record<string, unknown>>(
    mkt === "US" ? `/us/fundamentals/${sym}` : `/tw/fundamentals/${sym}`
  ).then((r) => r.data);

const fetchFinancials = (sym: string) =>
  api.get<{ symbol: string; source: string; data: unknown }>(`/us/financials/${sym}`)
    .then((r) => r.data);

const fetchOptions = (sym: string, expiry?: string) =>
  api.get<OptionRow[]>(`/us/options/${sym}${expiry ? `?expiration_date=${expiry}` : ""}`)
    .then((r) => r.data);

const fetchInstitutional = (sym: string) =>
  api.get<InstitutionalRow[]>(`/tw/institutional/${sym}?days=60`).then((r) => r.data);

const fetchMargin = (sym: string) =>
  api.get<MarginRow[]>(`/tw/margin/${sym}?days=60`).then((r) => r.data);

const fetchRevenue = (sym: string) =>
  api.get<RevenueRow[]>(`/tw/revenue/${sym}?months=24`).then((r) => r.data);

const fetchHealth = (sym: string) =>
  api.get<HealthResponse>(`/tw/health/${sym}?periods=8`).then((r) => r.data);

const fetchValuationBand = (sym: string, metric: "pe" | "pb") =>
  api.get<ValuationBandResponse>(`/tw/valuation-band/${sym}?metric=${metric}&years=5`)
    .then((r) => r.data);

const fetchDividends = (sym: string) =>
  api.get<DividendRow[]>(`/tw/dividends/${sym}`).then((r) => r.data);

const fetchETFHoldings = (sym: string) =>
  api.get<ETFHoldingsResponse>(`/tw/etf/${sym}/holdings`).then((r) => r.data);

const fetchEarnings = (sym: string) =>
  api.get<{ earnings_date: string | null; eps_estimate: number | null; revenue_estimate: number | null }>(
    `/us/earnings/${sym}`
  ).then((r) => r.data);

// ── shared helpers ─────────────────────────────────────────────────

const fmt = (n: number | null | undefined, d = 2) =>
  n == null ? "—" : n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

const fmtPct = (n: number | null | undefined, alreadyPct = false) => {
  if (n == null) return "—";
  const v = alreadyPct ? n : n * 100;
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
};

const fmtK = (n: number) =>
  n >= 1e9 ? `${(n / 1e9).toFixed(2)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(0)}K` : String(n);

// ── reusable UI atoms ──────────────────────────────────────────────

function StatRow({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div className="flex justify-between items-center py-1.5 border-b border-border/50 last:border-0">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-xs text-foreground font-medium">{value ?? "—"}</span>
    </div>
  );
}

function TabButton({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`shrink-0 whitespace-nowrap px-3 sm:px-4 py-2 text-sm border-b-2 transition-colors ${
        active
          ? "border-primary text-foreground font-medium"
          : "border-transparent text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );
}

function PeriodButton({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`px-2.5 py-1 text-xs rounded transition-colors ${
        active ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );
}

function Loading() {
  const { t } = useTranslation();
  return <div className="p-8 text-center text-muted-foreground text-sm animate-pulse">{t("common.loading")}</div>;
}

// ── US tab panels ──────────────────────────────────────────────────

function FinancialsPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const { data, isLoading } = useQuery({
    queryKey: ["financials", "US", symbol],
    queryFn: () => fetchFinancials(symbol),
    staleTime: 3_600_000,
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

// ── IV Surface heatmap ────────────────────────────────────────────

function IVSurface({ options, optionType }: { options: OptionRow[]; optionType: "call" | "put" }) {
  const filtered = options.filter(
    (o) => o.contract_type?.toLowerCase() === optionType && o.implied_volatility != null
  );
  if (!filtered.length) return null;

  const expiries = [...new Set(filtered.map((o) => o.expiration_date).filter(Boolean))].sort().slice(0, 8);
  const strikes = [...new Set(filtered.map((o) => o.strike))].sort((a, b) => a - b);
  // Take ~12 strikes centred around median
  const mid = Math.floor(strikes.length / 2);
  const slicedStrikes = strikes.slice(Math.max(0, mid - 6), mid + 6);

  // iv lookup
  const ivMap: Record<string, number> = {};
  filtered.forEach((o) => {
    if (o.expiration_date && o.strike) {
      ivMap[`${o.expiration_date}:${o.strike}`] = (o.implied_volatility ?? 0) * 100;
    }
  });

  const allIVs = Object.values(ivMap).filter(Boolean);
  const minIV = Math.min(...allIVs);
  const maxIV = Math.max(...allIVs);

  function ivColor(iv: number | undefined): string {
    if (!iv || maxIV === minIV) return "hsl(var(--card))";
    const t = (iv - minIV) / (maxIV - minIV);
    // low IV = blue, high IV = red
    const r = Math.round(t * 239);
    const g = Math.round((1 - Math.abs(t - 0.5) * 2) * 120);
    const b = Math.round((1 - t) * 239);
    return `rgb(${r},${g},${b})`;
  }

  if (!expiries.length || !slicedStrikes.length) return null;

  return (
    <div className="mt-4">
      <h4 className="text-xs font-semibold text-foreground mb-2">
        Implied Volatility Surface ({optionType.toUpperCase()})
      </h4>
      <div className="overflow-x-auto">
        <table className="text-xs border-collapse">
          <thead>
            <tr>
              <th className="text-left pr-3 pb-1 text-muted-foreground font-medium">Strike \ Expiry</th>
              {expiries.map((e) => (
                <th key={e} className="px-1 pb-1 text-muted-foreground font-medium text-center min-w-[60px]">
                  {e?.slice(5)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {slicedStrikes.map((strike) => (
              <tr key={strike}>
                <td className="pr-3 py-0.5 text-muted-foreground">{strike}</td>
                {expiries.map((expiry) => {
                  const iv = ivMap[`${expiry}:${strike}`];
                  return (
                    <td
                      key={expiry}
                      className="text-center py-0.5 px-1 rounded text-[10px] font-medium"
                      style={{
                        backgroundColor: ivColor(iv),
                        color: iv ? "#f9fafb" : "transparent",
                        minWidth: 52,
                      }}
                    >
                      {iv ? `${iv.toFixed(1)}%` : "·"}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        <div className="flex items-center gap-2 mt-2 text-[10px] text-muted-foreground">
          <span>Low IV</span>
          <div className="flex h-2 w-24 rounded overflow-hidden">
            {Array.from({ length: 24 }).map((_, i) => (
              <div key={i} style={{ flex: 1, backgroundColor: ivColor(minIV + (i / 23) * (maxIV - minIV)) }} />
            ))}
          </div>
          <span>High IV</span>
        </div>
      </div>
    </div>
  );
}

// ── Options panel ─────────────────────────────────────────────────

function OptionsPanel({ symbol }: { symbol: string }) {
  const [optionType, setOptionType] = useState<"call" | "put">("call");
  const [view, setView] = useState<"table" | "surface">("table");

  const { data: options = [], isLoading } = useQuery({
    queryKey: ["options", symbol],
    queryFn: () => fetchOptions(symbol),
    staleTime: 300_000,
  });

  if (isLoading) return <Loading />;
  if (!options.length) return <div className="p-6 text-muted-foreground text-sm">No options data available.</div>;

  // Get available expiry dates
  const expiries = [...new Set(options.map((o) => o.expiration_date).filter(Boolean))].sort();
  const filtered = options.filter((o) => o.contract_type?.toLowerCase() === optionType);

  return (
    <div className="p-4 space-y-4">
      {/* controls */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex rounded border border-border overflow-hidden text-sm">
          {(["call", "put"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setOptionType(t)}
              className={`px-4 py-1.5 transition-colors ${
                optionType === t
                  ? t === "call" ? "bg-green-900/40 text-green-400" : "bg-red-900/40 text-red-400"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {t.toUpperCase()}
            </button>
          ))}
        </div>
        <div className="flex rounded border border-border overflow-hidden text-sm">
          {(["table", "surface"] as const).map((v) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={`px-3 py-1.5 transition-colors ${
                view === v ? "bg-primary/20 text-primary" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {v === "table" ? "Table" : "IV Surface"}
            </button>
          ))}
        </div>
        <span className="text-xs text-muted-foreground">{filtered.length} contracts</span>
      </div>

      {/* expiry legend */}
      {view === "table" && expiries.length > 0 && (
        <div className="text-xs text-muted-foreground">
          Available expirations: {expiries.slice(0, 6).join(" · ")}{expiries.length > 6 ? " …" : ""}
        </div>
      )}

      {/* IV surface */}
      {view === "surface" && <IVSurface options={options} optionType={optionType} />}

      {/* table */}
      {view === "table" && <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left py-2 pr-3 font-medium">Expiry</th>
              <th className="text-right py-2 px-2 font-medium">Strike</th>
              <th className="text-right py-2 px-2 font-medium">Bid</th>
              <th className="text-right py-2 px-2 font-medium">Ask</th>
              <th className="text-right py-2 px-2 font-medium">Last</th>
              <th className="text-right py-2 px-2 font-medium">Volume</th>
              <th className="text-right py-2 px-2 font-medium">OI</th>
              <th className="text-right py-2 px-2 font-medium">IV</th>
              <th className="text-right py-2 px-2 font-medium">Delta</th>
            </tr>
          </thead>
          <tbody>
            {filtered.slice(0, 80).map((o, i) => (
              <tr key={i} className="border-b border-border/30 hover:bg-accent/5">
                <td className="py-1.5 pr-3 text-muted-foreground">{o.expiration_date ?? "—"}</td>
                <td className="text-right py-1.5 px-2 font-medium text-foreground">{fmt(o.strike)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmt(o.bid)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmt(o.ask)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmt(o.last_price)}</td>
                <td className="text-right py-1.5 px-2 text-muted-foreground">{o.volume ? fmtK(o.volume) : "—"}</td>
                <td className="text-right py-1.5 px-2 text-muted-foreground">{o.open_interest ? fmtK(o.open_interest) : "—"}</td>
                <td className="text-right py-1.5 px-2 text-muted-foreground">
                  {o.implied_volatility ? `${(o.implied_volatility * 100).toFixed(1)}%` : "—"}
                </td>
                <td className="text-right py-1.5 px-2 text-muted-foreground">{fmt(o.delta, 3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>}
    </div>
  );
}

// ── TW tab panels ──────────────────────────────────────────────────

function InstitutionalPanel({ symbol }: { symbol: string }) {
  const { data = [], isLoading } = useQuery({
    queryKey: ["institutional", symbol],
    queryFn: () => fetchInstitutional(symbol),
    staleTime: 300_000,
  });

  if (isLoading) return <Loading />;
  if (!data.length) return <div className="p-6 text-muted-foreground text-sm">No institutional data.</div>;

  const chartData = [...data].reverse().slice(-30).map((r) => ({
    date: r.date.slice(5),
    fini: Math.round((r.fini_buy - r.fini_sell) / 1000),
    sitc: Math.round((r.sitc_buy - r.sitc_sell) / 1000),
    dealer: Math.round((r.dealer_buy - r.dealer_sell) / 1000),
  }));

  return (
    <div className="p-4 space-y-4">
      <div className="space-y-4">
        {/* net buy bar chart */}
        <div>
          <h4 className="text-xs font-semibold text-foreground mb-2">外資淨買超（千股，近 30 日）</h4>
          <ResponsiveContainer width="100%" height={120}>
            <BarChart data={chartData} margin={{ left: 0, right: 0 }}>
              <XAxis dataKey="date" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} interval="preserveStartEnd" />
              <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} width={40} />
              <Tooltip contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }} />
              <ReferenceLine y={0} stroke="hsl(var(--border))" />
              <Bar dataKey="fini" name="外資" radius={[2, 2, 0, 0]}>
                {chartData.map((d, i) => (
                  <Cell key={i} fill={d.fini >= 0 ? "#22c55e" : "#ef4444"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* table */}
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="text-left py-2 pr-4 font-medium">日期</th>
                <th className="text-right py-2 px-2 font-medium">外資買</th>
                <th className="text-right py-2 px-2 font-medium">外資賣</th>
                <th className="text-right py-2 px-2 font-medium text-green-400">外資淨</th>
                <th className="text-right py-2 px-2 font-medium">投信買</th>
                <th className="text-right py-2 px-2 font-medium">投信賣</th>
                <th className="text-right py-2 px-2 font-medium text-blue-400">投信淨</th>
                <th className="text-right py-2 px-2 font-medium text-purple-400">自營淨</th>
              </tr>
            </thead>
            <tbody>
              {[...data].reverse().slice(0, 30).map((r) => {
                const finiNet = r.fini_buy - r.fini_sell;
                const sitcNet = r.sitc_buy - r.sitc_sell;
                const dealerNet = r.dealer_buy - r.dealer_sell;
                return (
                  <tr key={r.date} className="border-b border-border/30 hover:bg-accent/5">
                    <td className="py-1.5 pr-4 text-muted-foreground">{r.date}</td>
                    <td className="text-right py-1.5 px-2">{fmtK(r.fini_buy)}</td>
                    <td className="text-right py-1.5 px-2">{fmtK(r.fini_sell)}</td>
                    <td className={`text-right py-1.5 px-2 font-medium ${finiNet >= 0 ? "text-green-400" : "text-red-400"}`}>
                      {finiNet >= 0 ? "+" : ""}{fmtK(Math.abs(finiNet))}
                    </td>
                    <td className="text-right py-1.5 px-2">{fmtK(r.sitc_buy)}</td>
                    <td className="text-right py-1.5 px-2">{fmtK(r.sitc_sell)}</td>
                    <td className={`text-right py-1.5 px-2 font-medium ${sitcNet >= 0 ? "text-green-400" : "text-red-400"}`}>
                      {sitcNet >= 0 ? "+" : ""}{fmtK(Math.abs(sitcNet))}
                    </td>
                    <td className={`text-right py-1.5 px-2 font-medium ${dealerNet >= 0 ? "text-green-400" : "text-red-400"}`}>
                      {dealerNet >= 0 ? "+" : ""}{fmtK(Math.abs(dealerNet))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function MarginPanel({ symbol }: { symbol: string }) {
  const { data = [], isLoading } = useQuery({
    queryKey: ["margin", symbol],
    queryFn: () => fetchMargin(symbol),
    staleTime: 300_000,
  });

  if (isLoading) return <Loading />;
  if (!data.length) return <div className="p-6 text-muted-foreground text-sm">No margin data.</div>;

  const chartData = [...data].reverse().slice(-30).map((r) => ({
    date: r.date.slice(5),
    margin_balance: Math.round(r.margin_balance / 1000),
    short_balance: Math.round(r.short_balance / 1000),
  }));

  return (
    <div className="p-4 space-y-4">
      <div>
        <h4 className="text-xs font-semibold text-foreground mb-2">融資融券餘額（千股，近 30 日）</h4>
        <ResponsiveContainer width="100%" height={130}>
          <BarChart data={chartData} margin={{ left: 0, right: 0 }}>
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} width={45} />
            <Tooltip contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }} />
            <Bar dataKey="margin_balance" name="融資餘額" fill="#3b82f6" radius={[2, 2, 0, 0]} />
            <Bar dataKey="short_balance" name="融券餘額" fill="#f59e0b" radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left py-2 pr-4 font-medium">日期</th>
              <th className="text-right py-2 px-2 font-medium text-blue-400">融資買入</th>
              <th className="text-right py-2 px-2 font-medium text-blue-400">融資餘額</th>
              <th className="text-right py-2 px-2 font-medium text-yellow-400">融券賣出</th>
              <th className="text-right py-2 px-2 font-medium text-yellow-400">融券餘額</th>
              <th className="text-right py-2 px-2 font-medium">券資比</th>
            </tr>
          </thead>
          <tbody>
            {[...data].reverse().slice(0, 30).map((r) => {
              const ratio = r.margin_balance > 0 ? (r.short_balance / r.margin_balance) * 100 : null;
              return (
                <tr key={r.date} className="border-b border-border/30 hover:bg-accent/5">
                  <td className="py-1.5 pr-4 text-muted-foreground">{r.date}</td>
                  <td className="text-right py-1.5 px-2 text-blue-400">{fmtK(r.margin_purchase)}</td>
                  <td className="text-right py-1.5 px-2 text-blue-400 font-medium">{fmtK(r.margin_balance)}</td>
                  <td className="text-right py-1.5 px-2 text-yellow-400">{fmtK(r.short_sale)}</td>
                  <td className="text-right py-1.5 px-2 text-yellow-400 font-medium">{fmtK(r.short_balance)}</td>
                  <td className="text-right py-1.5 px-2 text-muted-foreground">
                    {ratio != null ? `${ratio.toFixed(1)}%` : "—"}
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

function RevenuePanel({ symbol }: { symbol: string }) {
  const { data = [], isLoading } = useQuery({
    queryKey: ["revenue", symbol],
    queryFn: () => fetchRevenue(symbol),
    staleTime: 3_600_000,
  });

  if (isLoading) return <Loading />;
  if (!data.length) return <div className="p-6 text-muted-foreground text-sm">No revenue data.</div>;

  const chartData = [...data].reverse().slice(-24).map((r) => ({
    date: r.date.slice(0, 7),
    revenue: Math.round(r.revenue / 1000),   // millions NTD
    yoy: r.revenue_yoy,
  }));

  return (
    <div className="p-4 space-y-4">
      <div>
        <h4 className="text-xs font-semibold text-foreground mb-2">月營收（百萬新台幣）</h4>
        <ResponsiveContainer width="100%" height={130}>
          <BarChart data={chartData} margin={{ left: 0, right: 0 }}>
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} width={50} />
            <Tooltip
              contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }}
              formatter={(v: number) => [`${v}M NTD`, "Revenue"]}
            />
            <Bar dataKey="revenue" name="月營收" fill="#6366f1" radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left py-2 pr-4 font-medium">月份</th>
              <th className="text-right py-2 px-2 font-medium">營收（千元）</th>
              <th className="text-right py-2 px-2 font-medium">月增率</th>
              <th className="text-right py-2 px-2 font-medium">年增率</th>
            </tr>
          </thead>
          <tbody>
            {[...data].reverse().slice(0, 24).map((r) => (
              <tr key={r.date} className="border-b border-border/30 hover:bg-accent/5">
                <td className="py-1.5 pr-4 text-muted-foreground">{r.date.slice(0, 7)}</td>
                <td className="text-right py-1.5 px-2 text-foreground font-medium">
                  {r.revenue.toLocaleString()}
                </td>
                <td className={`text-right py-1.5 px-2 font-medium ${
                  r.revenue_mom == null ? "text-muted-foreground"
                  : r.revenue_mom >= 0 ? "text-green-400" : "text-red-400"
                }`}>
                  {r.revenue_mom == null ? "—" : fmtPct(r.revenue_mom, true)}
                </td>
                <td className={`text-right py-1.5 px-2 font-medium ${
                  r.revenue_yoy == null ? "text-muted-foreground"
                  : r.revenue_yoy >= 0 ? "text-green-400" : "text-red-400"
                }`}>
                  {r.revenue_yoy == null ? "—" : fmtPct(r.revenue_yoy, true)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Financial health (財務體質) ────────────────────────────────────

const LIGHT_CLASS: Record<Light, string> = {
  green:  "bg-green-500",
  yellow: "bg-yellow-500",
  red:    "bg-red-500",
  gray:   "bg-muted-foreground/40",
};

function LightDot({ light }: { light: Light }) {
  return <span className={`inline-block w-2.5 h-2.5 rounded-full ${LIGHT_CLASS[light]}`} />;
}

function HealthSection({
  title, light, children,
}: { title: string; light: Light; children: React.ReactNode }) {
  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="flex items-center gap-2 mb-3">
        <LightDot light={light} />
        <h4 className="text-sm font-semibold text-foreground">{title}</h4>
      </div>
      {children}
    </div>
  );
}

const fmtPct1 = (n: number | null | undefined) =>
  n == null ? "—" : `${n >= 0 ? "" : ""}${n.toFixed(2)}%`;

function MetricSparkRow({
  label, periods, accessor, suffix = "%",
}: {
  label: string;
  periods: HealthPeriod[];
  accessor: (p: HealthPeriod) => number | null;
  suffix?: string;
}) {
  const values = periods.map(accessor);
  const latest = values[values.length - 1];
  const data = periods.map((p, i) => ({
    date: p.date.slice(0, 7),
    value: values[i],
  }));
  return (
    <div>
      <div className="flex items-baseline justify-between mb-1">
        <span className="text-xs text-muted-foreground">{label}</span>
        <span className="text-sm font-medium text-foreground">
          {latest == null ? "—" : `${latest.toFixed(2)}${suffix}`}
        </span>
      </div>
      <ResponsiveContainer width="100%" height={42}>
        <BarChart data={data} margin={{ left: 0, right: 0, top: 4, bottom: 0 }}>
          <XAxis dataKey="date" hide />
          <YAxis hide />
          <Tooltip
            contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }}
            formatter={(v: number) => [`${v == null ? "—" : v.toFixed(2)}${suffix}`, label]}
          />
          <Bar dataKey="value" radius={[2, 2, 0, 0]}>
            {data.map((d, i) => (
              <Cell
                key={i}
                fill={d.value == null
                  ? "hsl(var(--muted-foreground) / 0.2)"
                  : d.value >= 0 ? "#22c55e" : "#ef4444"}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function HealthPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const { data, isLoading } = useQuery({
    queryKey: ["health", symbol],
    queryFn: () => fetchHealth(symbol),
    staleTime: 6 * 3_600_000,    // 6 hours; quarterlies don't change often
  });

  if (isLoading) return <Loading />;
  if (!data || !data.periods.length) {
    return <div className="p-6 text-muted-foreground text-sm">{t("stock.health.no_data")}</div>;
  }

  const { periods, summary, lights } = data;

  return (
    <div className="p-4 space-y-4">
      {/* summary strip */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">ROE (TTM)</div>
          <div className="text-lg font-semibold text-foreground">{fmtPct1(summary.latest_roe)}</div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.health.debt_ratio")}</div>
          <div className="text-lg font-semibold text-foreground">{fmtPct1(summary.latest_debt_ratio)}</div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.health.gross_margin")}</div>
          <div className="text-lg font-semibold text-foreground">{fmtPct1(summary.latest_gross_margin)}</div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.health.revenue_yoy")}</div>
          <div className={`text-lg font-semibold ${
            summary.revenue_yoy == null ? "text-muted-foreground"
            : summary.revenue_yoy >= 0 ? "text-green-400" : "text-red-400"
          }`}>
            {fmtPct1(summary.revenue_yoy)}
          </div>
        </div>
      </div>

      {/* four StatementDog sections */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <HealthSection title={t("stock.health.profitability")} light={lights.profitability}>
          <div className="space-y-3">
            <MetricSparkRow label={t("stock.health.gross_margin")} periods={periods} accessor={(p) => p.gross_margin} />
            <MetricSparkRow label={t("stock.health.operating_margin")} periods={periods} accessor={(p) => p.operating_margin} />
            <MetricSparkRow label={t("stock.health.net_margin")} periods={periods} accessor={(p) => p.net_margin} />
          </div>
        </HealthSection>

        <HealthSection title={t("stock.health.safety")} light={lights.safety}>
          <div className="space-y-3">
            <MetricSparkRow label={t("stock.health.debt_ratio")} periods={periods} accessor={(p) => p.debt_ratio} />
            <MetricSparkRow
              label={t("stock.health.current_ratio")}
              periods={periods}
              accessor={(p) => p.current_ratio}
              suffix="x"
            />
          </div>
        </HealthSection>

        <HealthSection title={t("stock.health.growth")} light={lights.growth}>
          <MetricSparkRow
            label={t("stock.health.eps")}
            periods={periods}
            accessor={(p) => p.eps}
            suffix=""
          />
          <div className="mt-3 text-xs text-muted-foreground">
            {t("stock.health.revenue_yoy")}:{" "}
            <span className={`font-medium ${
              summary.revenue_yoy == null ? ""
              : summary.revenue_yoy >= 0 ? "text-green-400" : "text-red-400"
            }`}>
              {fmtPct1(summary.revenue_yoy)}
            </span>
          </div>
        </HealthSection>

        <HealthSection title={t("stock.health.cash_flow")} light={lights.cash_flow}>
          <MetricSparkRow
            label={t("stock.health.operating_cf")}
            periods={periods}
            accessor={(p) => p.operating_cf}
            suffix=""
          />
          <MetricSparkRow
            label={t("stock.health.free_cf")}
            periods={periods}
            accessor={(p) => p.free_cf}
            suffix=""
          />
          <div className="mt-2 text-xs text-muted-foreground">
            {t("stock.health.cf_streak", { count: summary.cf_positive_streak_4q })}
          </div>
        </HealthSection>
      </div>

      {/* per-period table */}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left py-2 pr-4 font-medium">{t("stock.health.period")}</th>
              <th className="text-right py-2 px-2 font-medium">{t("stock.health.gross_margin")}</th>
              <th className="text-right py-2 px-2 font-medium">{t("stock.health.operating_margin")}</th>
              <th className="text-right py-2 px-2 font-medium">{t("stock.health.net_margin")}</th>
              <th className="text-right py-2 px-2 font-medium">{t("stock.health.debt_ratio")}</th>
              <th className="text-right py-2 px-2 font-medium">EPS</th>
            </tr>
          </thead>
          <tbody>
            {[...periods].reverse().map((p) => (
              <tr key={p.date} className="border-b border-border/30 hover:bg-accent/5">
                <td className="py-1.5 pr-4 text-muted-foreground">{p.date}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmtPct1(p.gross_margin)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmtPct1(p.operating_margin)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmtPct1(p.net_margin)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmtPct1(p.debt_ratio)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{p.eps == null ? "—" : p.eps.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Valuation band (PE / PB 河流圖) ────────────────────────────────

function ValuationBandPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const [metric, setMetric] = useState<"pe" | "pb">("pe");
  const { data, isLoading } = useQuery({
    queryKey: ["valuation-band", symbol, metric],
    queryFn: () => fetchValuationBand(symbol, metric),
    staleTime: 6 * 3_600_000,
  });

  if (isLoading) return <Loading />;
  if (!data || !data.series.length) {
    return <div className="p-6 text-muted-foreground text-sm">{t("stock.valuation.no_data")}</div>;
  }

  const { series, stats } = data;
  const filtered = series.filter((s) => s.value != null);
  if (!filtered.length) {
    return <div className="p-6 text-muted-foreground text-sm">{t("stock.valuation.no_data")}</div>;
  }

  // Sample to ~250 points so Recharts stays smooth on 5y of daily data.
  const stride = Math.max(1, Math.floor(filtered.length / 250));
  const chartData = filtered.filter((_, i) => i % stride === 0);

  const mean = stats.mean ?? 0;
  const std = stats.std ?? 0;
  const bands = std > 0 ? [
    { v: mean + 2 * std, label: "+2σ", color: "#ef4444" },
    { v: mean + std,     label: "+1σ", color: "#f59e0b" },
    { v: mean,           label: "μ",   color: "#6366f1" },
    { v: mean - std,     label: "-1σ", color: "#10b981" },
    { v: mean - 2 * std, label: "-2σ", color: "#22c55e" },
  ] : [];

  // Percentile of `current` against history.
  let percentile: number | null = null;
  if (stats.current != null) {
    const sorted = [...filtered.map((s) => s.value as number)].sort((a, b) => a - b);
    const lessEq = sorted.filter((v) => v <= (stats.current as number)).length;
    percentile = (lessEq / sorted.length) * 100;
  }

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex rounded border border-border overflow-hidden">
          {(["pe", "pb"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMetric(m)}
              className={`px-4 py-1.5 text-sm transition-colors ${
                metric === m
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {m === "pe" ? t("stock.valuation.pe_band") : t("stock.valuation.pb_band")}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.valuation.current")}</div>
          <div className="text-lg font-semibold text-foreground">
            {stats.current == null ? "—" : stats.current.toFixed(2)}
          </div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.valuation.mean")}</div>
          <div className="text-lg font-semibold text-foreground">
            {stats.mean == null ? "—" : stats.mean.toFixed(2)}
          </div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.valuation.z_score")}</div>
          <div className={`text-lg font-semibold ${
            stats.current_z == null ? "text-muted-foreground"
            : stats.current_z > 1 ? "text-red-400"
            : stats.current_z < -1 ? "text-green-400"
            : "text-foreground"
          }`}>
            {stats.current_z == null ? "—" : stats.current_z.toFixed(2)}
          </div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.valuation.percentile")}</div>
          <div className="text-lg font-semibold text-foreground">
            {percentile == null ? "—" : `${percentile.toFixed(0)}%`}
          </div>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={320}>
        <LineChart data={chartData} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <XAxis
            dataKey="date"
            tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
            interval="preserveStartEnd"
            minTickGap={40}
          />
          <YAxis
            tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
            width={45}
            domain={["auto", "auto"]}
          />
          <Tooltip
            contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }}
            formatter={(v: number) => [v?.toFixed(2) ?? "—", metric.toUpperCase()]}
          />
          {bands.map((b) => (
            <ReferenceLine
              key={b.label}
              y={b.v}
              stroke={b.color}
              strokeDasharray="4 4"
              label={{ value: `${b.label} ${b.v.toFixed(1)}`, position: "right",
                       fill: b.color, fontSize: 10 }}
            />
          ))}
          <Line
            type="monotone"
            dataKey="value"
            stroke="#3b82f6"
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>

      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 text-xs">
        {([
          ["min", t("stock.valuation.min")],
          ["p25", "P25"],
          ["p50", t("stock.valuation.median")],
          ["p75", "P75"],
          ["max", t("stock.valuation.max")],
        ] as const).map(([k, label]) => (
          <div key={k} className="bg-background border border-border rounded px-2 py-1.5 flex justify-between">
            <span className="text-muted-foreground">{label}</span>
            <span className="text-foreground font-medium">
              {stats[k] == null ? "—" : (stats[k] as number).toFixed(2)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── ETF holdings ──────────────────────────────────────────────────

const PIE_COLORS = [
  "#3b82f6", "#8b5cf6", "#ec4899", "#f59e0b", "#10b981",
  "#06b6d4", "#6366f1", "#f43f5e", "#84cc16", "#eab308",
];

function HoldingsPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const { data, isLoading } = useQuery({
    queryKey: ["etf-holdings", symbol],
    queryFn: () => fetchETFHoldings(symbol),
    staleTime: 6 * 3_600_000,
  });

  if (isLoading) return <Loading />;
  if (!data || !data.holdings.length) {
    return (
      <div className="p-6 text-muted-foreground text-sm">
        {t("stock.etf.no_holdings")}
      </div>
    );
  }

  const top10 = data.holdings.slice(0, 10);
  const otherWeight = data.holdings.slice(10).reduce((s, h) => s + h.weight, 0);
  const pieData = otherWeight > 0
    ? [...top10.map((h) => ({ name: h.symbol, value: h.weight })),
       { name: t("stock.etf.others"), value: Number(otherWeight.toFixed(2)) }]
    : top10.map((h) => ({ name: h.symbol, value: h.weight }));

  const top10Weight = top10.reduce((s, h) => s + h.weight, 0);

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>{t("stock.etf.as_of", { date: data.as_of ?? "—" })}</span>
        <span>{t("stock.etf.top10_concentration")}: <span className="text-foreground font-medium">{top10Weight.toFixed(2)}%</span></span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        <div className="lg:col-span-2">
          <ResponsiveContainer width="100%" height={280}>
            <PieChart>
              <Pie
                data={pieData}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy="50%"
                outerRadius={100}
                innerRadius={45}
                paddingAngle={1}
              >
                {pieData.map((_, i) => (
                  <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }}
                formatter={(v: number) => [`${v.toFixed(2)}%`, "weight"]}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} iconSize={8} />
            </PieChart>
          </ResponsiveContainer>
        </div>

        <div className="lg:col-span-3 overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="text-left py-2 pr-4 font-medium">#</th>
                <th className="text-left py-2 pr-4 font-medium">{t("stock.etf.constituent")}</th>
                <th className="text-left py-2 pr-4 font-medium">{t("market.table.name")}</th>
                <th className="text-right py-2 px-2 font-medium">{t("stock.etf.weight")}</th>
              </tr>
            </thead>
            <tbody>
              {data.holdings.slice(0, 30).map((h, i) => (
                <tr key={h.symbol} className="border-b border-border/30 hover:bg-accent/5">
                  <td className="py-1.5 pr-4 text-muted-foreground">{i + 1}</td>
                  <td className="py-1.5 pr-4 text-primary font-medium">{h.symbol}</td>
                  <td className="py-1.5 pr-4 text-foreground">{h.name_zh || "—"}</td>
                  <td className="text-right py-1.5 px-2 text-foreground font-medium">
                    {h.weight.toFixed(2)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ── Dividend history ──────────────────────────────────────────────

function DividendsPanel({ symbol, currentPrice }: { symbol: string; currentPrice: number | null }) {
  const { t } = useTranslation();
  const { data = [], isLoading } = useQuery({
    queryKey: ["dividends", symbol],
    queryFn: () => fetchDividends(symbol),
    staleTime: 6 * 3_600_000,
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
          <Tooltip
            contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }}
            formatter={(v: number) => [v.toFixed(3), "TWD/share"]}
          />
          <Legend wrapperStyle={{ fontSize: 11 }} iconSize={8} />
          <Bar dataKey="cash" name={t("stock.dividends.cash")} fill="#22c55e" radius={[2, 2, 0, 0]} />
          <Bar dataKey="stock" name={t("stock.dividends.stock")} fill="#f59e0b" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left py-2 pr-4 font-medium">{t("stock.dividends.announce_date")}</th>
              <th className="text-left py-2 pr-4 font-medium">{t("stock.dividends.ex_date")}</th>
              <th className="text-right py-2 px-2 font-medium">{t("stock.dividends.cash")} (TWD)</th>
              <th className="text-right py-2 px-2 font-medium">{t("stock.dividends.stock")} (股)</th>
            </tr>
          </thead>
          <tbody>
            {[...data].reverse().slice(0, 30).map((r, i) => (
              <tr key={`${r.date}-${i}`} className="border-b border-border/30 hover:bg-accent/5">
                <td className="py-1.5 pr-4 text-muted-foreground">{r.date}</td>
                <td className="py-1.5 pr-4 text-muted-foreground">{r.ex_date ?? "—"}</td>
                <td className="text-right py-1.5 px-2 text-green-400 font-medium">
                  {r.cash_dividend ? r.cash_dividend.toFixed(3) : "—"}
                </td>
                <td className="text-right py-1.5 px-2 text-yellow-400">
                  {r.stock_dividend ? r.stock_dividend.toFixed(3) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── News feed ─────────────────────────────────────────────────────

interface NewsItem {
  title: string;
  publisher: string;
  link: string;
  published_at: string;
  thumbnail: string | null;
}

function NewsFeed({ symbol, market }: { symbol: string; market: "US" | "TW" | "CRYPTO" }) {
  const { t } = useTranslation();
  const prefix = market === "US" ? "us" : market === "CRYPTO" ? "crypto" : "tw";
  const { data: items = [], isLoading } = useQuery<NewsItem[]>({
    queryKey: ["news", market, symbol],
    queryFn: () => api.get(`/${prefix}/news/${symbol}`).then((r) => r.data),
    staleTime: 5 * 60_000,
  });

  if (isLoading) {
    return (
      <div className="bg-card border border-border rounded-lg p-4 text-xs text-muted-foreground animate-pulse">
        {t("common.loading")}
      </div>
    );
  }
  if (!items.length) {
    return (
      <div className="bg-card border border-border rounded-lg p-4 text-xs text-muted-foreground">
        No recent news found.
      </div>
    );
  }

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden divide-y divide-border">
      {items.map((item, i) => (
        <a
          key={i}
          href={item.link}
          target="_blank"
          rel="noopener noreferrer"
          className="flex gap-3 p-3 hover:bg-accent/5 transition-colors"
        >
          {item.thumbnail && (
            <img
              src={item.thumbnail}
              alt=""
              className="w-16 h-12 object-cover rounded shrink-0"
              onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
            />
          )}
          <div className="flex-1 min-w-0 space-y-1">
            <p className="text-sm font-medium leading-snug line-clamp-2">{item.title}</p>
            <p className="text-xs text-muted-foreground">
              {item.publisher} ·{" "}
              {new Date(item.published_at).toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
                year: "numeric",
              })}
            </p>
          </div>
        </a>
      ))}
    </div>
  );
}

// ── main page ──────────────────────────────────────────────────────

export default function StockDetailPage() {
  const { t } = useTranslation();
  const { market = "US", symbol = "" } = useParams<{ market: string; symbol: string }>();
  const navigate = useNavigate();
  const mkt = market.toUpperCase() as Market;
  const sym = symbol.toUpperCase();

  const [period, setPeriod] = useState<Period>("1y");
  const [usTab, setUsTab] = useState<USTab>("chart");
  const [twTab, setTwTab] = useState<TWTab>("chart");
  const [cryptoTab, setCryptoTab] = useState<CryptoTab>("chart");
  const [livePrice, setLivePrice] = useState<number | null>(null);
  const [liveChange, setLiveChange] = useState<number | null>(null);

  const { data: quote } = useQuery({
    queryKey: ["quote", mkt, sym],
    queryFn: () => fetchQuote(mkt, sym),
    staleTime: 15_000,
  });

  const { data: bars = [], isLoading: barsLoading } = useQuery({
    queryKey: ["history", mkt, sym, period],
    queryFn: () => fetchHistory(mkt, sym, period),
    staleTime: 60_000,
    enabled: mkt === "CRYPTO" ? true
      : mkt === "US" ? usTab === "chart" : twTab === "chart",
  });

  const { data: fundamentals } = useQuery({
    queryKey: ["fundamentals", mkt, sym],
    queryFn: () => fetchFundamentals(mkt, sym),
    staleTime: 3_600_000,
  });

  const { data: earnings } = useQuery({
    queryKey: ["earnings", sym],
    queryFn: () => fetchEarnings(sym),
    staleTime: 6 * 3_600_000,
    enabled: mkt === "US",
  });

  useWebSocket(`${sym}:${mkt}`, (data: unknown) => {
    const d = data as Record<string, number>;
    if (d.price) setLivePrice(d.price);
    if (d.change_pct !== undefined) setLiveChange(d.change_pct);
  });

  const displayPrice = livePrice ?? (quote?.price as number | undefined);
  const displayChange = liveChange ?? (quote?.change_pct as number | undefined);
  const isPositive = (displayChange ?? 0) >= 0;

  const isETF = mkt === "TW" && (Boolean(quote?.is_etf) || isTWETF(sym));

  const showChart =
    mkt === "CRYPTO" ? cryptoTab === "chart"
    : mkt === "US" ? usTab === "chart"
    : twTab === "chart";

  return (
    <div className="p-4 sm:p-6 space-y-4 sm:space-y-5">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <button onClick={() => navigate(`/market/${mkt}`)} className="hover:text-foreground transition-colors">
          {mkt === "US" ? t("nav.us_market")
            : mkt === "CRYPTO" ? t("nav.crypto")
            : t("nav.tw_market")}
        </button>
        <span>/</span>
        <span className="text-foreground font-medium">{sym}</span>
      </div>

      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl sm:text-2xl font-bold text-foreground">{sym}</h1>
          {Boolean(quote?.name || quote?.name_zh) && (
            <p className="text-xs sm:text-sm text-muted-foreground mt-0.5 truncate">
              {String(quote?.name ?? quote?.name_zh ?? "")}
              {mkt === "TW" && Boolean(quote?.exchange) && (
                <span className="ml-2 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded">
                  {String(quote?.exchange ?? "")}
                </span>
              )}
              {isETF && (
                <span className="ml-2 text-xs bg-amber-500/15 text-amber-500 px-1.5 py-0.5 rounded font-medium">
                  ETF
                </span>
              )}
            </p>
          )}
        </div>
        <div className="text-right shrink-0">
          <div className="text-2xl sm:text-3xl font-bold text-foreground tabular-nums">
            {displayPrice !== undefined ? fmt(displayPrice) : "—"}
          </div>
          <div className={`text-xs sm:text-sm font-medium tabular-nums ${isPositive ? "text-green-400" : "text-red-400"}`}>
            {displayChange !== undefined ? fmtPct(displayChange) : "—"}
          </div>
          <div className="text-[10px] sm:text-xs text-muted-foreground mt-0.5">
            {mkt === "TW" ? "TWD" : (quote?.currency as string ?? "USD")}
          </div>
        </div>
      </div>

      {/* Tab strip — horizontal scroll on small screens so 6-8 TW tabs
          don't wrap or overflow. */}
      <div className="flex border-b border-border overflow-x-auto -mx-4 px-4 sm:mx-0 sm:px-0">
        {mkt === "US" ? (
          <>
            <TabButton active={usTab === "chart"} label={t("stock.history")} onClick={() => setUsTab("chart")} />
            <TabButton active={usTab === "financials"} label={t("stock.fundamentals")} onClick={() => setUsTab("financials")} />
            <TabButton active={usTab === "options"} label={t("stock.options")} onClick={() => setUsTab("options")} />
            <TabButton active={usTab === "news"} label={t("stock.news")} onClick={() => setUsTab("news")} />
          </>
        ) : mkt === "CRYPTO" ? (
          <>
            <TabButton active={cryptoTab === "chart"} label={t("stock.history")} onClick={() => setCryptoTab("chart")} />
            <TabButton active={cryptoTab === "news"} label={t("stock.news")} onClick={() => setCryptoTab("news")} />
          </>
        ) : isETF ? (
          <>
            <TabButton active={twTab === "chart"} label={t("stock.history")} onClick={() => setTwTab("chart")} />
            <TabButton active={twTab === "holdings"} label={t("stock.etf.holdings_tab")} onClick={() => setTwTab("holdings")} />
            <TabButton active={twTab === "dividends"} label={t("stock.dividends.tab")} onClick={() => setTwTab("dividends")} />
            <TabButton active={twTab === "institutional"} label="法人買賣超" onClick={() => setTwTab("institutional")} />
            <TabButton active={twTab === "margin"} label="融資融券" onClick={() => setTwTab("margin")} />
            <TabButton active={twTab === "news"} label={t("stock.news")} onClick={() => setTwTab("news")} />
          </>
        ) : (
          <>
            <TabButton active={twTab === "chart"} label={t("stock.history")} onClick={() => setTwTab("chart")} />
            <TabButton active={twTab === "health"} label={t("stock.health.tab")} onClick={() => setTwTab("health")} />
            <TabButton active={twTab === "valuation"} label={t("stock.valuation.tab")} onClick={() => setTwTab("valuation")} />
            <TabButton active={twTab === "dividends"} label={t("stock.dividends.tab")} onClick={() => setTwTab("dividends")} />
            <TabButton active={twTab === "institutional"} label="法人買賣超" onClick={() => setTwTab("institutional")} />
            <TabButton active={twTab === "margin"} label="融資融券" onClick={() => setTwTab("margin")} />
            <TabButton active={twTab === "revenue"} label="月營收" onClick={() => setTwTab("revenue")} />
            <TabButton active={twTab === "news"} label={t("stock.news")} onClick={() => setTwTab("news")} />
          </>
        )}
      </div>

      {showChart && (
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-5">
          {/* chart */}
          <div className="lg:col-span-3 bg-card border border-border rounded-lg overflow-hidden">
            <div className="flex items-center gap-1 px-4 pt-3 pb-2 border-b border-border">
              {(["1d", "5d", "1mo", "3mo", "1y", "5y"] as Period[]).map((p) => (
                <PeriodButton key={p} active={p === period} label={p} onClick={() => setPeriod(p)} />
              ))}
            </div>
            <div className="p-3">
              {barsLoading ? (
                <div className="h-[360px] flex items-center justify-center text-muted-foreground text-sm animate-pulse">
                  {t("common.loading")}
                </div>
              ) : bars.length === 0 ? (
                <div className="h-[360px] flex items-center justify-center text-muted-foreground text-sm">
                  No data available
                </div>
              ) : (
                <CandlestickChart bars={bars} height={360} />
              )}
            </div>
          </div>

          {/* stats sidebar */}
          <div className="bg-card border border-border rounded-lg p-4">
            <h3 className="text-sm font-semibold text-foreground mb-3">
              {t("stock.fundamentals")}
            </h3>
            {mkt === "US" ? (
              <>
                <StatRow label="Market Cap" value={
                  quote?.market_cap
                    ? `$${((quote.market_cap as number) / 1e9).toFixed(2)}B`
                    : "—"
                } />
                <StatRow label="P/E Ratio" value={fmt(fundamentals?.pe_ratio as number | null)} />
                <StatRow label="P/B Ratio" value={fmt(fundamentals?.pb_ratio as number | null)} />
                <StatRow label="EPS" value={fmt(fundamentals?.eps as number | null)} />
                <StatRow label="Beta" value={fmt(fundamentals?.beta as number | null)} />
                <StatRow label="Div. Yield" value={
                  fundamentals?.dividend_yield
                    ? `${((fundamentals.dividend_yield as number) * 100).toFixed(2)}%`
                    : "—"
                } />
                <StatRow label="52W High" value={fmt(fundamentals?.fifty_two_week_high as number | null)} />
                <StatRow label="52W Low" value={fmt(fundamentals?.fifty_two_week_low as number | null)} />
                <StatRow label="Sector" value={fundamentals?.sector as string | null} />
                {earnings?.earnings_date && (
                  <>
                    <StatRow label="Next Earnings" value={earnings.earnings_date} />
                    {earnings.eps_estimate != null && (
                      <StatRow label="EPS Est." value={fmt(earnings.eps_estimate)} />
                    )}
                    {earnings.revenue_estimate != null && (
                      <StatRow
                        label="Rev. Est."
                        value={earnings.revenue_estimate >= 1e9
                          ? `$${(earnings.revenue_estimate / 1e9).toFixed(2)}B`
                          : `$${(earnings.revenue_estimate / 1e6).toFixed(0)}M`}
                      />
                    )}
                  </>
                )}
              </>
            ) : (
              <>
                <StatRow label="開盤" value={fmt(quote?.open as number | null)} />
                <StatRow label="最高" value={fmt(quote?.high as number | null)} />
                <StatRow label="最低" value={fmt(quote?.low as number | null)} />
                <StatRow label="昨收" value={fmt(quote?.prev_close as number | null)} />
                <StatRow label="本益比 P/E" value={fmt(fundamentals?.pe_ratio as number | null)} />
                <StatRow label="淨值比 P/B" value={fmt(fundamentals?.pb_ratio as number | null)} />
                <StatRow label="殖利率" value={
                  fundamentals?.dividend_yield != null
                    ? `${(fundamentals.dividend_yield as number).toFixed(2)}%`
                    : "—"
                } />
                <StatRow label="成交量" value={quote?.volume ? (quote.volume as number).toLocaleString() : "—"} />
                <StatRow label="市場" value={quote?.exchange as string | null} />
              </>
            )}
            <StatRow label="Volume" value={quote?.volume ? (quote.volume as number).toLocaleString() : "—"} />
          </div>
        </div>
      )}

      {mkt === "US" && usTab === "financials" && (
        <div className="bg-card border border-border rounded-lg overflow-hidden">
          <FinancialsPanel symbol={sym} />
        </div>
      )}
      {mkt === "US" && usTab === "options" && (
        <div className="bg-card border border-border rounded-lg overflow-hidden">
          <OptionsPanel symbol={sym} />
        </div>
      )}

      {mkt === "TW" ? (
        <>
          {twTab === "health" && (
            <div className="bg-card border border-border rounded-lg overflow-hidden">
              <HealthPanel symbol={sym} />
            </div>
          )}
          {twTab === "valuation" && (
            <div className="bg-card border border-border rounded-lg overflow-hidden">
              <ValuationBandPanel symbol={sym} />
            </div>
          )}
          {twTab === "holdings" && (
            <div className="bg-card border border-border rounded-lg overflow-hidden">
              <HoldingsPanel symbol={sym} />
            </div>
          )}
          {twTab === "dividends" && (
            <div className="bg-card border border-border rounded-lg overflow-hidden">
              <DividendsPanel symbol={sym} currentPrice={(quote?.price as number | undefined) ?? null} />
            </div>
          )}
          {twTab === "institutional" && (
            <div className="bg-card border border-border rounded-lg overflow-hidden">
              <InstitutionalPanel symbol={sym} />
            </div>
          )}
          {twTab === "margin" && (
            <div className="bg-card border border-border rounded-lg overflow-hidden">
              <MarginPanel symbol={sym} />
            </div>
          )}
          {twTab === "revenue" && (
            <div className="bg-card border border-border rounded-lg overflow-hidden">
              <RevenuePanel symbol={sym} />
            </div>
          )}
          {twTab === "news" && <NewsFeed symbol={sym} market="TW" />}
        </>
      ) : mkt === "CRYPTO" ? (
        cryptoTab === "news" ? <NewsFeed symbol={sym} market="CRYPTO" /> : null
      ) : (
        usTab === "news" ? <NewsFeed symbol={sym} market="US" /> : null
      )}

      {mkt === "US" && usTab === "chart" && Boolean(fundamentals?.description) && (
        <div className="bg-card border border-border rounded-lg p-4">
          <h3 className="text-sm font-semibold text-foreground mb-2">About</h3>
          <p className="text-xs text-muted-foreground leading-relaxed line-clamp-4">
            {String(fundamentals?.description ?? "")}
          </p>
        </div>
      )}
    </div>
  );
}
