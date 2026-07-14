import { useEffect, useRef, useState } from "react";
import { useAuthStore } from "@/store/authStore";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  Bitcoin,
  Bot,
  Globe,
  Landmark,
  LineChart,
  PieChart,
  Search,
  Star,
  TrendingUp,
  type LucideIcon,
} from "lucide-react";
import api from "@/lib/api";
import { formatQuoteFreshness } from "@/lib/freshness";
import { DataSourceBadge } from "@/components/DataSourceBadge";
import { DeltaText, Num } from "@/components/Num";
import { useLiveQuote, useWsConnected } from "@/hooks/useWebSocket";
import { IntelFeed } from "@/components/dashboard/IntelFeed";

// ── Market indices ─────────────────────────────────────────────────

function IndexCard({ symbol, label }: { symbol: string; label: string }) {
  const { i18n } = useTranslation();
  // WS-aware polling (PR-9): the card subscribes to the symbol's live
  // WS stream via the rAF-batched quoteStore. While the socket is up
  // AND a tick has landed for this symbol, the 30 s REST poll is
  // paused — the WS delta path is fresher and cheaper. If the socket
  // drops (or never authenticates), polling resumes automatically.
  const live = useLiveQuote(symbol, "US");
  const wsConnected = useWsConnected();
  const wsLive = wsConnected && live?.price != null;
  const { data } = useQuery({
    queryKey: ["quote", "US", symbol],
    queryFn: () => api.get<Record<string, unknown>>(`/us/quote/${symbol}`).then((r) => r.data),
    staleTime: 30_000,
    refetchInterval: wsLive ? false : 30_000,
  });

  // Live-vs-REST precedence mirrors LiveQuoteHeader: the latest WS
  // value wins as soon as it exists; the REST snapshot covers the gap
  // before the first tick and off-hours.
  const price = live?.price ?? (data?.price as number | undefined);
  const changePct = live?.changePct ?? (data?.change_pct as number | undefined);
  // Backend returns data_source="unavailable" + price=0 when every
  // upstream fails. Render that as "—" instead of a misleading
  // 0.00 / +0.00% green pill that looks like the asset is at zero.
  const dataSource = live?.dataSource ?? (data?.data_source as string | undefined);
  const unavailable = live?.price == null && data?.data_source === "unavailable";
  const tsMs = live?.ts ?? (data?.ts as number | undefined);
  // Date-aware: "14:31" today, "4/29 14:31" any earlier day. Stops
  // weekend / off-hours views from looking like live prices.
  const localTime = formatQuoteFreshness(tsMs ?? null, i18n.language);

  // Tick flash — briefly tint the price row when a refetch moves the
  // price. Keyframes live in index.css (`animate-flash-up/down`, 300 ms,
  // disabled under prefers-reduced-motion). The ref tracks the last
  // rendered price so a cache-identical refetch doesn't flash.
  const prevPrice = useRef<number | null>(null);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);
  useEffect(() => {
    if (unavailable || price == null) return;
    const prev = prevPrice.current;
    prevPrice.current = price;
    if (prev == null || prev === price) return;
    setFlash(price > prev ? "up" : "down");
    const id = setTimeout(() => setFlash(null), 400);
    return () => clearTimeout(id);
  }, [price, unavailable]);

  return (
    <Link
      to={`/stock/US/${symbol}`}
      className="block shrink-0 snap-start min-w-[44%] sm:min-w-0 bg-surface-1 shadow-highlight border border-border rounded-lg p-4 hover:border-primary/40 transition-colors"
    >
      <div className="text-label uppercase text-muted-foreground mb-1 truncate">{label}</div>
      <div
        className={`text-stat font-semibold tabular-nums text-foreground rounded px-1 -mx-1 ${
          flash === "up" ? "animate-flash-up" : flash === "down" ? "animate-flash-down" : ""
        }`}
        aria-live="polite"
      >
        {!unavailable && price != null ? <Num value={price} /> : "—"}
      </div>
      <div className="text-data font-medium mt-0.5">
        {unavailable ? (
          <span className="text-muted-foreground">—</span>
        ) : (
          <DeltaText value={changePct} percent />
        )}
      </div>
      <div className="text-micro text-muted-foreground mt-1 inline-flex items-center">
        {symbol}
        <DataSourceBadge source={dataSource} />
      </div>
      {localTime && (
        <div className="text-micro text-muted-foreground/70 mt-0.5 tabular-nums">
          {localTime}
        </div>
      )}
    </Link>
  );
}

// ── Recent news ────────────────────────────────────────────────────

// ── Main ──────────────────────────────────────────────────────────

interface QuickAccessCard {
  labelKey: string;
  descKey: string;
  icon: LucideIcon;
  /** Left accent — full literal class names so Tailwind JIT sees them. */
  accent: string;
  iconCls: string;
  href: string;
}

export default function DashboardPage() {
  const { t } = useTranslation();
  const { user } = useAuthStore();

  const indices = [
    { symbol: "SPY", label: t("dashboard.indices.sp500") },
    { symbol: "QQQ", label: t("dashboard.indices.nasdaq100") },
    { symbol: "IWM", label: t("dashboard.indices.russell2000") },
    { symbol: "GLD", label: t("dashboard.indices.gold_etf") },
  ];

  const cards: QuickAccessCard[] = [
    { labelKey: "nav.us_market", descKey: "dashboard.cards.us_market_desc", icon: TrendingUp, accent: "border-l-chart-1 hover:border-l-chart-1", iconCls: "text-chart-1", href: "/market/US" },
    { labelKey: "nav.tw_market", descKey: "dashboard.cards.tw_market_desc", icon: Landmark,   accent: "border-l-chart-2 hover:border-l-chart-2", iconCls: "text-chart-2", href: "/market/TW" },
    { labelKey: "nav.crypto",    descKey: "dashboard.cards.crypto_desc",    icon: Bitcoin,    accent: "border-l-chart-3 hover:border-l-chart-3", iconCls: "text-chart-3", href: "/market/CRYPTO" },
    { labelKey: "nav.screener",  descKey: "dashboard.cards.screener_desc",  icon: Search,     accent: "border-l-chart-4 hover:border-l-chart-4", iconCls: "text-chart-4", href: "/screener" },
    { labelKey: "nav.watchlist", descKey: "dashboard.cards.watchlist_desc", icon: Star,       accent: "border-l-chart-5 hover:border-l-chart-5", iconCls: "text-chart-5", href: "/watchlist" },
    { labelKey: "nav.portfolio", descKey: "dashboard.cards.portfolio_desc", icon: PieChart,   accent: "border-l-chart-6 hover:border-l-chart-6", iconCls: "text-chart-6", href: "/portfolio" },
    { labelKey: "nav.analytics", descKey: "dashboard.cards.analytics_desc", icon: LineChart,  accent: "border-l-chart-1 hover:border-l-chart-1", iconCls: "text-chart-1", href: "/analytics" },
    { labelKey: "nav.macro",     descKey: "dashboard.cards.macro_desc",     icon: Globe,      accent: "border-l-chart-2 hover:border-l-chart-2", iconCls: "text-chart-2", href: "/macro" },
    { labelKey: "nav.ai",        descKey: "dashboard.cards.ai_desc",        icon: Bot,        accent: "border-l-chart-3 hover:border-l-chart-3", iconCls: "text-chart-3", href: "/ai" },
  ];

  return (
    <div className="p-gutter sm:p-page space-y-stack sm:space-y-section">
      {/* status bar — single-line: title · user · role chip · AI quota */}
      <div className="flex items-center gap-2 flex-wrap min-w-0 bg-surface-1 shadow-highlight border border-border-subtle rounded-lg px-3 py-2">
        <h1 className="text-heading font-bold text-foreground shrink-0">{t("dashboard.title")}</h1>
        <span className="text-muted-foreground/50 shrink-0">·</span>
        <span className="text-data text-muted-foreground truncate min-w-0">{user?.email}</span>
        {user?.role && (
          <span className="shrink-0 px-1.5 py-0.5 rounded border border-primary/30 bg-primary/10 text-primary text-label uppercase">
            {user.role}
          </span>
        )}
        {user?.ai_requests_remaining !== undefined && (
          <span className="ml-auto shrink-0 text-label uppercase text-muted-foreground">
            {t("dashboard.ai_requests_today")}{" "}
            <span className="text-foreground font-mono tabular-nums text-data normal-case">
              {user.ai_requests_remaining}
            </span>{" "}
            {t("dashboard.remaining")}
          </span>
        )}
      </div>

      {/* market indices — horizontal snap ticker on mobile, 4-up grid ≥sm */}
      <div>
        <h2 className="text-label text-muted-foreground uppercase mb-2">{t("dashboard.market_overview")}</h2>
        <div className="flex gap-3 overflow-x-auto snap-x pb-1 sm:grid sm:grid-cols-4 sm:overflow-visible sm:pb-0">
          {indices.map((idx) => (
            <IndexCard key={idx.symbol} symbol={idx.symbol} label={idx.label} />
          ))}
        </div>
      </div>

      {/* nav cards + intel feed. Mobile fold-order: intel feed (fresh info)
          before quick-access (whose 4 destinations live in the BottomNav). */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="order-2 lg:order-1 lg:col-span-2">
          <h2 className="text-label text-muted-foreground uppercase mb-2">{t("dashboard.quick_access")}</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {cards.map((card) => {
              const Icon = card.icon;
              return (
                <Link
                  key={card.labelKey}
                  to={card.href}
                  className={`bg-card shadow-highlight border border-border border-l-[3px] ${card.accent} rounded-lg p-4 space-y-1.5 hover:border-primary/50 hover:bg-card/80 transition-colors block group`}
                >
                  <Icon className={`w-4 h-4 ${card.iconCls}`} aria-hidden="true" />
                  <h2 className="text-foreground font-medium text-heading group-hover:text-primary transition-colors">
                    {t(card.labelKey)}
                  </h2>
                  <p className="text-data text-muted-foreground">{t(card.descKey)}</p>
                </Link>
              );
            })}
          </div>
        </div>

        <div className="order-1 lg:order-2">
          <IntelFeed />
        </div>
      </div>
    </div>
  );
}
