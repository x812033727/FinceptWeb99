import { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, Cell,
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
type TWTab = "chart" | "institutional" | "margin" | "revenue" | "news";

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
      className={`px-4 py-2 text-sm border-b-2 transition-colors ${
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

// ── News feed ─────────────────────────────────────────────────────

interface NewsItem {
  title: string;
  publisher: string;
  link: string;
  published_at: string;
  thumbnail: string | null;
}

function NewsFeed({ symbol, market }: { symbol: string; market: "US" | "TW" }) {
  const { t } = useTranslation();
  const { data: items = [], isLoading } = useQuery<NewsItem[]>({
    queryKey: ["news", market, symbol],
    queryFn: () =>
      api
        .get(`/${market === "US" ? "us" : "tw"}/news/${symbol}`)
        .then((r) => r.data),
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

  const showChart = mkt === "CRYPTO" ? true
    : mkt === "US" ? usTab === "chart" : twTab === "chart";

  return (
    <div className="p-6 space-y-5">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <button onClick={() => navigate(`/market/${mkt}`)} className="hover:text-foreground transition-colors">
          {mkt === "US" ? t("nav.us_market") : t("nav.tw_market")}
        </button>
        <span>/</span>
        <span className="text-foreground font-medium">{sym}</span>
      </div>

      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">{sym}</h1>
          {Boolean(quote?.name || quote?.name_zh) && (
            <p className="text-sm text-muted-foreground mt-0.5">
              {String(quote?.name ?? quote?.name_zh ?? "")}
              {mkt === "TW" && Boolean(quote?.exchange) && (
                <span className="ml-2 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded">
                  {String(quote?.exchange ?? "")}
                </span>
              )}
            </p>
          )}
        </div>
        <div className="text-right">
          <div className="text-3xl font-bold text-foreground">
            {displayPrice !== undefined ? fmt(displayPrice) : "—"}
          </div>
          <div className={`text-sm font-medium ${isPositive ? "text-green-400" : "text-red-400"}`}>
            {displayChange !== undefined ? fmtPct(displayChange) : "—"}
          </div>
          <div className="text-xs text-muted-foreground mt-0.5">
            {mkt === "US" ? (quote?.currency as string ?? "USD") : "TWD"}
          </div>
        </div>
      </div>

      <div className="flex border-b border-border">
        {mkt === "US" ? (
          <>
            <TabButton active={usTab === "chart"} label={t("stock.history")} onClick={() => setUsTab("chart")} />
            <TabButton active={usTab === "financials"} label={t("stock.fundamentals")} onClick={() => setUsTab("financials")} />
            <TabButton active={usTab === "options"} label={t("stock.options")} onClick={() => setUsTab("options")} />
            <TabButton active={usTab === "news"} label={t("stock.news")} onClick={() => setUsTab("news")} />
          </>
        ) : mkt === "CRYPTO" ? (
          <>
            {/* Crypto: only the chart tab makes sense — financials / options /
                institutional / margin all assume an equity issuer. News could
                come from CryptoPanic etc. but is out of scope (per plan). */}
            <TabButton active={true} label={t("stock.history")} onClick={() => {}} />
          </>
        ) : (
          <>
            <TabButton active={twTab === "chart"} label={t("stock.history")} onClick={() => setTwTab("chart")} />
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
