import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Maximize2, Minimize2 } from "lucide-react";
import CandlestickChart from "@/components/charts/CandlestickChart";
import type { Market } from "@/types/market";
import { aggregateBars } from "@/lib/aggregateBars";
import { PeriodButton, StatRow } from "@/components/stock/_atoms";
import { LiveQuoteHeader } from "@/components/stock/LiveQuoteHeader";
import { TabStrip, type TabDef } from "@/components/stock/TabStrip";
import TimeframeSelector from "@/components/stock/TimeframeSelector";
import {
  fetchEarnings,
  fetchFundamentals,
  fetchHistory,
  fetchIntraday,
  fetchQuote,
  fmt,
  isIntradayTimeframe,
  isTWETF,
  type Timeframe,
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
import { StockAIReportPanel } from "@/components/stock/StockAIReportPanel";
import { ValuationBandPanel } from "@/components/stock/ValuationBandPanel";

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
  // A3 fullscreen chart mode. Implemented with CSS (`fixed inset-0 z-50`)
  // rather than the Fullscreen API — the CSS approach works on iOS Safari
  // (which lacks requestFullscreen on arbitrary elements) and can't be
  // rejected by the browser. ESC exits; body scroll is locked while open.
  const [chartFullscreen, setChartFullscreen] = useState(false);
  useEffect(() => {
    if (!chartFullscreen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setChartFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [chartFullscreen]);

  // Live tick state (price / change / source / ts) intentionally does
  // NOT live in this component anymore. WS deltas flow through the
  // rAF-batched quoteStore and are consumed by <LiveQuoteHeader> via a
  // per-symbol selector, so a tick re-renders only that header block —
  // never this page body (K-line container, tabs, panels).
  const { data: quote } = useQuery({
    queryKey: ["quote", mkt, sym],
    queryFn: () => fetchQuote(mkt, sym),
    staleTime: 15_000,
  });

  const { data: bars = [], isLoading: barsLoading } = useQuery({
    queryKey: ["history", mkt, sym, period],
    queryFn: () => fetchHistory(mkt, sym, period),
    staleTime: 60_000,
    // Period is part of the queryKey, so switching 1d/5d/1mo/… used to
    // blank the chart while the new range loaded. keepPreviousData keeps
    // the old bars on screen until the new ones arrive.
    placeholderData: keepPreviousData,
    enabled: mkt === "CRYPTO" ? true
      : mkt === "US" ? usTab === "chart" : twTab === "chart",
  });

  // ── A2 多週期切換 ─────────────────────────────────────────────
  // 分時 (1m/5m/15m) fetches the snapshot-aggregated /intraday endpoint;
  // 週/月 aggregate the daily history above client-side. Default 日 keeps
  // the pre-A2 behaviour (daily bars rendered as-is).
  const [timeframe, setTimeframe] = useState<Timeframe>("1d");
  // Symbol/market navigation doesn't remount this page (same route), so
  // an intraday timeframe must not leak onto a symbol without snapshots.
  // Adjust-state-during-render (not an effect) per React guidance — the
  // stale render is discarded before the DOM commits.
  const [tfKey, setTfKey] = useState(`${mkt}:${sym}`);
  if (tfKey !== `${mkt}:${sym}`) {
    setTfKey(`${mkt}:${sym}`);
    setTimeframe("1d");
  }
  const intradayActive = isIntradayTimeframe(timeframe);

  const chartVisible =
    mkt === "CRYPTO" ? cryptoTab === "chart"
    : mkt === "US" ? usTab === "chart" : twTab === "chart";

  // Availability probe (5m) — enables/disables the 分時 buttons and doubles
  // as the data query when 5m is the selected interval (same queryKey).
  const { data: intradayProbe } = useQuery({
    queryKey: ["intraday", mkt, sym, "5m"],
    queryFn: () => fetchIntraday(mkt, sym, "5m"),
    staleTime: 60_000,
    enabled: chartVisible,
  });
  const intradayAvailable = (intradayProbe?.bars?.length ?? 0) > 0;
  const coverageDays = intradayProbe?.coverage_days ?? 30;

  const { data: intraday, isLoading: intradayLoading } = useQuery({
    queryKey: ["intraday", mkt, sym, timeframe],
    queryFn: () => fetchIntraday(mkt, sym, timeframe as "1m" | "5m" | "15m"),
    staleTime: 60_000,
    placeholderData: keepPreviousData,
    enabled: chartVisible && intradayActive,
  });

  // No manual useMemo — the React Compiler memoizes this; 週/月 rollup
  // over ≤ a few thousand daily bars is cheap even unmemoized.
  const displayBars = intradayActive
    ? intraday?.bars ?? []
    : timeframe === "1wk" ? aggregateBars(bars, "week")
    : timeframe === "1mo" ? aggregateBars(bars, "month")
    : bars;
  const displayLoading = intradayActive
    ? intradayLoading && !intraday
    : barsLoading;

  const { data: fundamentals } = useQuery({
    queryKey: ["fundamentals", mkt, sym],
    queryFn: () => fetchFundamentals(mkt, sym),
    staleTime: 3_600_000,
    gcTime: 24 * 3_600_000, // fundamentals tier — cache for a day
  });

  const { data: earnings } = useQuery({
    queryKey: ["earnings", sym],
    queryFn: () => fetchEarnings(sym),
    staleTime: 6 * 3_600_000,
    gcTime: 24 * 3_600_000, // fundamentals tier — cache for a day
    enabled: mkt === "US",
  });

  const isETF = mkt === "TW" && (Boolean(quote?.is_etf) || isTWETF(sym));

  const showChart =
    mkt === "CRYPTO" ? cryptoTab === "chart"
    : mkt === "US" ? usTab === "chart"
    : twTab === "chart";

  const tabDefs: TabDef[] = (() => {
    if (mkt === "US") {
      return [
        { key: "chart", label: t("stock.history"), active: usTab === "chart", onClick: () => setUsTab("chart") },
        { key: "financials", label: t("stock.fundamentals"), active: usTab === "financials", onClick: () => setUsTab("financials") },
        { key: "options", label: t("stock.options"), active: usTab === "options", onClick: () => setUsTab("options") },
        { key: "news", label: t("stock.news"), active: usTab === "news", onClick: () => setUsTab("news") },
        { key: "ai_report", label: t("stock.ai_report.tab"), active: usTab === "ai_report", onClick: () => setUsTab("ai_report") },
      ];
    }
    if (mkt === "CRYPTO") {
      return [
        { key: "chart", label: t("stock.history"), active: cryptoTab === "chart", onClick: () => setCryptoTab("chart") },
        { key: "news", label: t("stock.news"), active: cryptoTab === "news", onClick: () => setCryptoTab("news") },
      ];
    }
    if (isETF) {
      return [
        { key: "chart", label: t("stock.history"), active: twTab === "chart", onClick: () => setTwTab("chart") },
        { key: "holdings", label: t("stock.etf.holdings_tab"), active: twTab === "holdings", onClick: () => setTwTab("holdings") },
        { key: "dividends", label: t("stock.dividends.tab"), active: twTab === "dividends", onClick: () => setTwTab("dividends") },
        { key: "institutional", label: t("stock.tab_institutional"), active: twTab === "institutional", onClick: () => setTwTab("institutional") },
        { key: "margin", label: t("stock.tab_margin"), active: twTab === "margin", onClick: () => setTwTab("margin") },
        { key: "news", label: t("stock.news"), active: twTab === "news", onClick: () => setTwTab("news") },
        { key: "ai_report", label: t("stock.ai_report.tab"), active: twTab === "ai_report", onClick: () => setTwTab("ai_report") },
      ];
    }
    return [
      { key: "chart", label: t("stock.history"), active: twTab === "chart", onClick: () => setTwTab("chart") },
      { key: "health", label: t("stock.health.tab"), active: twTab === "health", onClick: () => setTwTab("health") },
      { key: "valuation", label: t("stock.valuation.tab"), active: twTab === "valuation", onClick: () => setTwTab("valuation") },
      { key: "dividends", label: t("stock.dividends.tab"), active: twTab === "dividends", onClick: () => setTwTab("dividends") },
      { key: "institutional", label: t("stock.tab_institutional"), active: twTab === "institutional", onClick: () => setTwTab("institutional") },
      { key: "margin", label: t("stock.tab_margin"), active: twTab === "margin", onClick: () => setTwTab("margin") },
      { key: "revenue", label: t("stock.tab_revenue"), active: twTab === "revenue", onClick: () => setTwTab("revenue") },
      { key: "news", label: t("stock.news"), active: twTab === "news", onClick: () => setTwTab("news") },
      { key: "ai_report", label: t("stock.ai_report.tab"), active: twTab === "ai_report", onClick: () => setTwTab("ai_report") },
    ];
  })();

  return (
    <div className="p-gutter sm:p-page space-y-stack sm:space-y-section">
      <div className="flex items-center gap-2 text-micro text-muted-foreground">
        <button onClick={() => navigate(`/market/${mkt}`)} className="hover:text-foreground transition-colors">
          {mkt === "US" ? t("nav.us_market")
            : mkt === "CRYPTO" ? t("nav.crypto")
            : t("nav.tw_market")}
        </button>
        <span>/</span>
        <span className="text-foreground font-medium">{sym}</span>
      </div>

      <LiveQuoteHeader symbol={sym} market={mkt} quote={quote} isETF={isETF} />

      {/* TabStrip collapses tabs 5+ into a "More" dropdown below md
          so 8-tab TW stocks don't force horizontal scroll on phones.
          Active tab is preserved in the visible row even if it would
          otherwise sit in the overflow zone. */}
      <TabStrip tabs={tabDefs} />

      {showChart && (
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-5">
          {/* chart */}
          <div
            className={
              chartFullscreen
                ? "fixed inset-0 z-50 bg-background flex flex-col"
                : "lg:col-span-3 bg-card shadow-highlight border border-border rounded-lg overflow-hidden"
            }
          >
            <div className="flex flex-wrap items-center gap-x-1 gap-y-1 px-4 pt-3 pb-2 border-b border-border shrink-0">
              {/* Range buttons apply to the daily-history timeframes only —
                  分時 always spans the whole snapshot coverage window, so
                  they hide while an intraday timeframe is active. TW's
                  history endpoint is daily-only: hide `1d` / `5d` so the
                  buttons can't render dead. US + Crypto get the full range. */}
              {!intradayActive &&
                ((mkt === "TW"
                  ? ["1mo", "3mo", "1y", "5y"]
                  : ["1d", "5d", "1mo", "3mo", "1y", "5y"]) as Period[]).map((p) => (
                  <PeriodButton key={p} active={p === period} label={p} onClick={() => setPeriod(p)} />
                ))}
              {!intradayActive && <span className="w-px h-4 bg-border mx-1" aria-hidden />}
              <TimeframeSelector
                value={timeframe}
                onChange={setTimeframe}
                intradayAvailable={intradayAvailable}
                coverageDays={coverageDays}
              />
              {intradayActive && (
                <span className="text-[11px] text-muted-foreground ml-2">
                  {t("stock.timeframe.coverage_note", { days: coverageDays })}
                </span>
              )}
              <button
                type="button"
                onClick={() => setChartFullscreen((f) => !f)}
                className="ml-auto p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-accent/20 transition-colors"
                aria-label={chartFullscreen ? t("stock.exit_fullscreen") : t("stock.fullscreen")}
                title={chartFullscreen ? t("stock.exit_fullscreen") : t("stock.fullscreen")}
              >
                {chartFullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
              </button>
            </div>
            <div className={chartFullscreen ? "p-3 flex-1 min-h-0" : "p-3"}>
              {displayLoading ? (
                <div className="h-full min-h-[360px] flex items-center justify-center text-muted-foreground text-sm animate-pulse">
                  {t("common.loading")}
                </div>
              ) : displayBars.length === 0 ? (
                <div className="h-full min-h-[360px] flex items-center justify-center text-muted-foreground text-sm">
                  {intradayActive
                    ? t("stock.timeframe.no_intraday_data")
                    : "No data available"}
                </div>
              ) : (
                // Omitting `height` lets the chart's ResizeObserver track the
                // flex container, so the canvas fills the viewport in
                // fullscreen and snaps back to 360px on exit.
                <CandlestickChart bars={displayBars} height={chartFullscreen ? undefined : 360} />
              )}
            </div>
          </div>

          {/* stats sidebar */}
          <div className="bg-card shadow-highlight border border-border rounded-lg p-4">
            <h3 className="text-heading font-semibold text-foreground mb-3">
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
                <StatRow label={t("stock.stat.open")} value={fmt(quote?.open as number | null)} />
                <StatRow label={t("stock.stat.high")} value={fmt(quote?.high as number | null)} />
                <StatRow label={t("stock.stat.low")} value={fmt(quote?.low as number | null)} />
                <StatRow label={t("stock.stat.prev_close")} value={fmt(quote?.prev_close as number | null)} />
                <StatRow label={t("stock.stat.pe")} value={fmt(fundamentals?.pe_ratio as number | null)} />
                <StatRow label={t("stock.stat.pb")} value={fmt(fundamentals?.pb_ratio as number | null)} />
                <StatRow label={t("stock.stat.dividend_yield")} value={
                  fundamentals?.dividend_yield != null
                    ? `${(fundamentals.dividend_yield as number).toFixed(2)}%`
                    : "—"
                } />
                <StatRow label={t("stock.stat.volume")} value={quote?.volume ? (quote.volume as number).toLocaleString() : "—"} />
                <StatRow label={t("stock.stat.market")} value={quote?.exchange as string | null} />
              </>
            )}
            <StatRow label="Volume" value={quote?.volume ? (quote.volume as number).toLocaleString() : "—"} />
          </div>
        </div>
      )}

      {mkt === "US" && usTab === "financials" && (
        <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
          <FinancialsPanel symbol={sym} />
        </div>
      )}
      {mkt === "US" && usTab === "options" && (
        <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
          <OptionsPanel symbol={sym} />
        </div>
      )}
      {mkt === "US" && usTab === "ai_report" && (
        <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
          <StockAIReportPanel symbol={sym} market="US" />
        </div>
      )}

      {mkt === "TW" ? (
        <>
          {twTab === "health" && (
            <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
              <HealthPanel symbol={sym} />
            </div>
          )}
          {twTab === "valuation" && (
            <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
              <ValuationBandPanel symbol={sym} />
            </div>
          )}
          {twTab === "holdings" && (
            <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
              <HoldingsPanel symbol={sym} />
            </div>
          )}
          {twTab === "dividends" && (
            <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
              <DividendsPanel symbol={sym} currentPrice={(quote?.price as number | undefined) ?? null} />
            </div>
          )}
          {twTab === "institutional" && (
            <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
              <InstitutionalPanel symbol={sym} />
            </div>
          )}
          {twTab === "margin" && (
            <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
              <MarginPanel symbol={sym} />
            </div>
          )}
          {twTab === "revenue" && (
            <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
              <RevenuePanel symbol={sym} />
            </div>
          )}
          {twTab === "news" && <NewsFeed symbol={sym} market="TW" />}
          {twTab === "ai_report" && (
            <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
              <StockAIReportPanel symbol={sym} market="TW" />
            </div>
          )}
        </>
      ) : mkt === "CRYPTO" ? (
        cryptoTab === "news" ? <NewsFeed symbol={sym} market="CRYPTO" /> : null
      ) : (
        usTab === "news" ? <NewsFeed symbol={sym} market="US" /> : null
      )}

      {mkt === "US" && usTab === "chart" && Boolean(fundamentals?.description) && (
        <div className="bg-card shadow-highlight border border-border rounded-lg p-4">
          <h3 className="text-heading font-semibold text-foreground mb-2">About</h3>
          <p className="text-xs text-muted-foreground leading-relaxed line-clamp-4">
            {String(fundamentals?.description ?? "")}
          </p>
        </div>
      )}
    </div>
  );
}
