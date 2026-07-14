import { useTranslation } from "react-i18next";
import { DataSourceBadge } from "@/components/DataSourceBadge";
import { useLiveQuote } from "@/hooks/useWebSocket";
import { formatQuoteFreshness } from "@/lib/freshness";
import type { Market } from "@/types/market";
import { fmt, fmtPct } from "@/components/stock/_shared";

interface LiveQuoteHeaderProps {
  symbol: string;
  market: Market;
  /** REST-fetched quote snapshot — fallback for initial paint before the
   *  first WS tick, and source of static fields (name, exchange, currency). */
  quote: Record<string, unknown> | undefined;
  isETF: boolean;
}

/**
 * Price header for StockDetailPage: symbol + name badges on the left,
 * big live price / change / freshness on the right.
 *
 * Extracted from the page body (blueprint §4.6) so WS ticks re-render
 * ONLY this block: it is the sole subscriber of `useLiveQuote`, and the
 * page keeps no per-tick state — the K-line chart container and the
 * stats sidebar no longer re-render on every delta.
 *
 * Live-vs-REST precedence per field mirrors the old page logic:
 * the latest WS value wins as soon as it exists; the REST snapshot
 * covers the gap during the WS auth handshake (~5 s) and off-hours.
 */
export function LiveQuoteHeader({ symbol, market, quote, isETF }: LiveQuoteHeaderProps) {
  const { t, i18n } = useTranslation();
  const live = useLiveQuote(symbol, market);

  const displayPrice = live?.price ?? (quote?.price as number | undefined);
  const displayChange = live?.changePct ?? (quote?.change_pct as number | undefined);
  const isPositive = (displayChange ?? 0) >= 0;
  // Latest data_source from the WS delta overrides the REST snapshot's
  // source so the hero badge updates as soon as the upstream changes
  // mid-session (e.g. Polygon recovers, primary→fallback switch).
  const source = live?.dataSource ?? (quote?.data_source as string | undefined);

  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl sm:text-2xl font-bold text-foreground inline-flex items-center">
          {symbol}
          <DataSourceBadge source={source} />
        </h1>
        {Boolean(quote?.name || quote?.name_zh) && (
          <p className="text-xs sm:text-sm text-muted-foreground mt-0.5 truncate">
            {String(quote?.name ?? quote?.name_zh ?? "")}
            {market === "TW" && Boolean(quote?.exchange) && (
              <span className="ml-2 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded">
                {String(quote?.exchange ?? "")}
              </span>
            )}
            {isETF && (
              <span className="ml-2 text-xs bg-warning/15 text-warning px-1.5 py-0.5 rounded font-medium">
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
        <div className={`text-xs sm:text-sm font-medium tabular-nums ${isPositive ? "text-up" : "text-down"}`}>
          {displayChange !== undefined ? fmtPct(displayChange, true) : "—"}
        </div>
        <div className="text-micro sm:text-xs text-muted-foreground mt-0.5">
          {market === "TW" ? "TWD" : (quote?.currency as string ?? "USD")}
        </div>
        {(() => {
          // Prefer the WS-driven ts (sub-second freshness during an
          // active session); fall back to the REST snapshot's `ts`
          // (set inside _normalize_quote at fetch time). Hide the
          // line entirely if neither path produced a number — better
          // than rendering "—" next to a real-looking price. Date
          // prefix ("M/D HH:MM:SS") appears only when the quote
          // isn't from today, so off-hours / weekend views aren't
          // ambiguous about which day the price belongs to.
          const tsMs = live?.ts ?? (quote?.ts as number | undefined);
          const localTime = formatQuoteFreshness(
            tsMs ?? null, i18n.language, { seconds: true },
          );
          if (!localTime) return null;
          return (
            <div className="text-micro text-muted-foreground/70 mt-0.5 tabular-nums">
              {t("stock.quoted_at")}：{localTime}
            </div>
          );
        })()}
      </div>
    </div>
  );
}
