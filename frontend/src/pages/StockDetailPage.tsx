import { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { formatQuoteFreshness } from "@/lib/freshness";
import CandlestickChart from "@/components/charts/CandlestickChart";
import { DataSourceBadge } from "@/components/DataSourceBadge";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { Market } from "@/types/market";
import { PeriodButton, StatRow, TabButton } from "@/components/stock/_atoms";
import {
  fetchEarnings,
  fetchFundamentals,
  fetchHistory,
  fetchQuote,
  fmt,
  fmtPct,
  isTWETF,
} from "@/components/stock/_shared";
import type { CryptoTab, Period, TWTab, USTab } from "@/components/stock/_shared";
import { DividendsPanel } from "@/components/stock/DividendsPanel";
import { FinancialsPanel } from "@/components/stock/FinancialsPanel";
import { HealthPanel } from "@/components/stock/HealthPanel";
import { HoldingsPanel } from "@/components/stock/HoldingsPanel";
import { InstitutionalPanel } from "@/components/stock/InstitutionalPanel";
import { MarginPanel } from "@/components/stock/MarginPanel";
import { NewsFeed } from "@/components/stock/NewsFeed";
import { OptionsPanel } from "@/components/stock/OptionsPanel";
import { RevenuePanel } from "@/components/stock/RevenuePanel";
import { ValuationBandPanel } from "@/components/stock/ValuationBandPanel";

export default function StockDetailPage() {
  const { t, i18n } = useTranslation();
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
  // Latest data_source from the WS delta — overrides the REST snapshot's
  // source so the hero badge updates as soon as the upstream changes
  // mid-session (e.g. Polygon recovers, primary→fallback switch).
  const [liveSource, setLiveSource] = useState<string | null>(null);
  // Latest quote timestamp (epoch ms). Lets the header show "資料時間
  // HH:MM:SS" so users can tell if they're looking at fresh ticks vs
  // a stale REST snapshot served during the WS connection's first
  // 5-second auth handshake.
  const [liveTs, setLiveTs] = useState<number | null>(null);

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
    const d = data as Record<string, number | string>;
    if (typeof d.price === "number" && d.price) setLivePrice(d.price);
    if (typeof d.change_pct === "number") setLiveChange(d.change_pct);
    if (typeof d.data_source === "string") setLiveSource(d.data_source);
    if (typeof d.ts === "number") setLiveTs(d.ts);
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
          <h1 className="text-xl sm:text-2xl font-bold text-foreground inline-flex items-center">
            {sym}
            <DataSourceBadge source={liveSource ?? (quote?.data_source as string | undefined)} />
          </h1>
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
            {displayChange !== undefined ? fmtPct(displayChange, true) : "—"}
          </div>
          <div className="text-[10px] sm:text-xs text-muted-foreground mt-0.5">
            {mkt === "TW" ? "TWD" : (quote?.currency as string ?? "USD")}
          </div>
          {(() => {
            // Prefer the WS-driven liveTs (sub-second freshness during
            // an active session); fall back to the REST snapshot's `ts`
            // (set inside _normalize_quote at fetch time). Hide the
            // line entirely if neither path produced a number — better
            // than rendering "—" next to a real-looking price. Date
            // prefix ("M/D HH:MM:SS") appears only when the quote
            // isn't from today, so off-hours / weekend views aren't
            // ambiguous about which day the price belongs to.
            const tsMs = liveTs ?? (quote?.ts as number | undefined);
            const localTime = formatQuoteFreshness(
              tsMs ?? null, i18n.language, { seconds: true },
            );
            if (!localTime) return null;
            return (
              <div className="text-[10px] text-muted-foreground/70 mt-0.5 tabular-nums">
                {t("stock.quoted_at")}：{localTime}
              </div>
            );
          })()}
        </div>
      </div>

      {/* Tab strip — horizontal scroll on small screens so 6-8 TW tabs
          don't wrap or overflow. The right-edge fade is a visual hint
          that more tabs are reachable beyond the cut-off; sm:hidden
          since the full strip fits at >=640px. */}
      <div className="relative -mx-4 sm:mx-0">
        <div className="flex border-b border-border overflow-x-auto px-4 sm:px-0">
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
        <div
          aria-hidden="true"
          className="pointer-events-none absolute top-0 right-0 h-full w-8 bg-gradient-to-l from-background to-transparent sm:hidden"
        />
      </div>

      {showChart && (
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-5">
          {/* chart */}
          <div className="lg:col-span-3 bg-card border border-border rounded-lg overflow-hidden">
            <div className="flex items-center gap-1 px-4 pt-3 pb-2 border-b border-border">
              {/* TW data is daily-only (no intraday endpoint) — hide
                  `1d` / `5d` so the buttons can't render dead. US +
                  Crypto get the full range. */}
              {((mkt === "TW"
                ? ["1mo", "3mo", "1y", "5y"]
                : ["1d", "5d", "1mo", "3mo", "1y", "5y"]) as Period[]).map((p) => (
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
