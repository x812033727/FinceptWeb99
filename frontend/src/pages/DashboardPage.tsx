import { useAuthStore } from "@/store/authStore";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { formatPct } from "@/lib/formatters";
import { formatQuoteFreshness } from "@/lib/freshness";
import { DataSourceBadge } from "@/components/DataSourceBadge";

// ── Market indices ─────────────────────────────────────────────────

function IndexCard({ symbol, label }: { symbol: string; label: string }) {
  const { i18n } = useTranslation();
  const { data } = useQuery({
    queryKey: ["quote", "US", symbol],
    queryFn: () => api.get<Record<string, unknown>>(`/us/quote/${symbol}`).then((r) => r.data),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });

  const price = data?.price as number | undefined;
  const changePct = data?.change_pct as number | undefined;
  const isPos = (changePct ?? 0) >= 0;
  // Backend returns data_source="unavailable" + price=0 when every
  // upstream fails. Render that as "—" instead of a misleading
  // 0.00 / +0.00% green pill that looks like the asset is at zero.
  const unavailable = data?.data_source === "unavailable";
  const tsMs = data?.ts as number | undefined;
  // Date-aware: "14:31" today, "4/29 14:31" any earlier day. Stops
  // weekend / off-hours views from looking like live prices.
  const localTime = formatQuoteFreshness(tsMs ?? null, i18n.language);

  return (
    <Link
      to={`/stock/US/${symbol}`}
      className="bg-card border border-border rounded-lg p-4 hover:border-primary/40 transition-colors"
    >
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <div className="text-lg font-bold text-foreground">
        {!unavailable && price != null
          ? price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
          : "—"}
      </div>
      <div className={`text-sm font-medium mt-0.5 ${unavailable ? "text-muted-foreground" : isPos ? "text-green-400" : "text-red-400"}`}>
        {unavailable ? "—" : formatPct(changePct)}
      </div>
      <div className="text-xs text-muted-foreground mt-1 inline-flex items-center">
        {symbol}
        <DataSourceBadge source={data?.data_source as string | undefined} />
      </div>
      {localTime && (
        <div className="text-[10px] text-muted-foreground/70 mt-0.5 tabular-nums">
          {localTime}
        </div>
      )}
    </Link>
  );
}

// ── Recent news ────────────────────────────────────────────────────

interface NewsItem {
  title: string;
  publisher: string;
  link: string;
  published_at: string;
  // Sentiment fields are populated only by the TW news endpoint
  // (`/tw/news/recent`). US news is read-through; sentiment scoring
  // only runs on rows that the TW ingest task wrote into the DB.
  sentiment_score?: number | null;
  sentiment_label?: "bullish" | "bearish" | "neutral" | null;
}

const SENTIMENT_BADGE: Record<
  NonNullable<NewsItem["sentiment_label"]>,
  { label: string; cls: string }
> = {
  bullish: { label: "利多", cls: "bg-green-900/30 text-green-300 border-green-800/50" },
  bearish: { label: "利空", cls: "bg-red-900/30 text-red-300 border-red-800/50" },
  neutral: { label: "中性", cls: "bg-zinc-800/40 text-zinc-300 border-zinc-700/50" },
};

function NewsList({ items }: { items: NewsItem[] }) {
  const { i18n } = useTranslation();
  return (
    <div className="divide-y divide-border/50">
      {items.slice(0, 5).map((item, i) => {
        const badge = item.sentiment_label
          ? SENTIMENT_BADGE[item.sentiment_label]
          : null;
        return (
          <a
            key={i}
            href={item.link}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-start gap-3 px-4 py-3 hover:bg-accent/5 transition-colors"
          >
            <div className="flex-1 min-w-0">
              <p className="text-sm text-foreground leading-snug line-clamp-2">{item.title}</p>
              <div className="mt-1 flex items-center gap-1.5 flex-wrap text-xs text-muted-foreground">
                <span>{item.publisher}</span>
                <span>·</span>
                <span>
                  {new Date(item.published_at).toLocaleDateString(i18n.language, {
                    month: "short", day: "numeric",
                  })}
                </span>
                {badge && (
                  <span
                    className={`px-1.5 py-0.5 rounded border text-[10px] ${badge.cls}`}
                  >
                    {badge.label}
                  </span>
                )}
              </div>
            </div>
          </a>
        );
      })}
    </div>
  );
}

function RecentGlobalNews() {
  const { t } = useTranslation();
  const { data: items = [], isLoading } = useQuery<NewsItem[]>({
    queryKey: ["news", "GLOBAL", "recent"],
    queryFn: () => api.get("/global/news/recent?limit=20").then((r) => r.data),
    staleTime: 5 * 60_000,
  });

  if (isLoading) {
    return <div className="text-xs text-muted-foreground animate-pulse">{t("dashboard.loading_news")}</div>;
  }

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h2 className="text-sm font-medium text-foreground">
          {t("dashboard.global_market_news")}
        </h2>
      </div>
      {!items.length ? (
        <div className="px-4 py-6 text-xs text-muted-foreground text-center">
          {t("dashboard.no_news")}
        </div>
      ) : (
        <NewsList items={items} />
      )}
    </div>
  );
}

function RecentTWNews() {
  const { t } = useTranslation();
  const { data: items = [], isLoading } = useQuery<NewsItem[]>({
    queryKey: ["news", "TW", "recent"],
    // 5-min stale matches the ingest cadence (hourly) — UI doesn't
    // need to refetch faster than the data can change.
    queryFn: () => api.get("/tw/news/recent?limit=20").then((r) => r.data),
    staleTime: 5 * 60_000,
  });

  if (isLoading) {
    return <div className="text-xs text-muted-foreground animate-pulse">{t("dashboard.loading_news")}</div>;
  }

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h2 className="text-sm font-medium text-foreground">
          {t("dashboard.tw_market_news")}
        </h2>
      </div>
      {!items.length ? (
        <div className="px-4 py-6 text-xs text-muted-foreground text-center">
          {t("dashboard.no_news")}
        </div>
      ) : (
        <NewsList items={items} />
      )}
    </div>
  );
}

// ── Main ──────────────────────────────────────────────────────────

export default function DashboardPage() {
  const { t } = useTranslation();
  const { user } = useAuthStore();

  const indices = [
    { symbol: "SPY", label: t("dashboard.indices.sp500") },
    { symbol: "QQQ", label: t("dashboard.indices.nasdaq100") },
    { symbol: "IWM", label: t("dashboard.indices.russell2000") },
    { symbol: "GLD", label: t("dashboard.indices.gold_etf") },
  ];

  const cards = [
    { labelKey: "nav.us_market",  descKey: "dashboard.cards.us_market_desc",  icon: "🇺🇸", href: "/market/US" },
    { labelKey: "nav.tw_market",  descKey: "dashboard.cards.tw_market_desc",  icon: "🇹🇼", href: "/market/TW" },
    { labelKey: "nav.crypto",     descKey: "dashboard.cards.crypto_desc",     icon: "₿",  href: "/market/CRYPTO" },
    { labelKey: "nav.screener",   descKey: "dashboard.cards.screener_desc",   icon: "🔍", href: "/screener" },
    { labelKey: "nav.watchlist",  descKey: "dashboard.cards.watchlist_desc",  icon: "⭐", href: "/watchlist" },
    { labelKey: "nav.portfolio",  descKey: "dashboard.cards.portfolio_desc",  icon: "📊", href: "/portfolio" },
    { labelKey: "nav.analytics",  descKey: "dashboard.cards.analytics_desc",  icon: "📐", href: "/analytics" },
    { labelKey: "nav.macro",      descKey: "dashboard.cards.macro_desc",      icon: "🌐", href: "/macro" },
    { labelKey: "nav.ai",         descKey: "dashboard.cards.ai_desc",         icon: "🤖", href: "/ai" },
  ];

  return (
    <div className="p-4 sm:p-6 space-y-5 sm:space-y-6">
      {/* welcome */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
        <div className="min-w-0">
          <h1 className="text-xl sm:text-2xl font-bold text-foreground">{t("dashboard.title")}</h1>
          <p className="text-xs sm:text-sm text-muted-foreground mt-0.5 truncate">
            {t("dashboard.welcome_back")}，<span className="text-foreground">{user?.email}</span>
            {" · "}
            <span className="capitalize text-primary">{user?.role}</span>
          </p>
        </div>
        {user?.ai_requests_remaining !== undefined && (
          <div className="text-xs text-muted-foreground sm:text-right shrink-0">
            {t("dashboard.ai_requests_today")}
            <div className="text-foreground font-medium text-sm">{user.ai_requests_remaining} {t("dashboard.remaining")}</div>
          </div>
        )}
      </div>

      {/* market indices */}
      <div>
        <h2 className="text-xs text-muted-foreground uppercase tracking-wider mb-2">{t("dashboard.market_overview")}</h2>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {indices.map((idx) => (
            <IndexCard key={idx.symbol} symbol={idx.symbol} label={idx.label} />
          ))}
        </div>
      </div>

      {/* nav cards + news */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <h2 className="text-xs text-muted-foreground uppercase tracking-wider mb-2">{t("dashboard.quick_access")}</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {cards.map((card) => (
              <Link
                key={card.labelKey}
                to={card.href}
                className="bg-card border border-border rounded-lg p-4 space-y-1.5 hover:border-primary/50 hover:bg-card/80 transition-colors block group"
              >
                <div className="text-xl">{card.icon}</div>
                <h2 className="text-foreground font-medium text-sm group-hover:text-primary transition-colors">
                  {t(card.labelKey)}
                </h2>
                <p className="text-xs text-muted-foreground">{t(card.descKey)}</p>
              </Link>
            ))}
          </div>
        </div>

        <div className="space-y-4">
          <div>
            <h2 className="text-xs text-muted-foreground uppercase tracking-wider mb-2">
              {t("dashboard.latest_tw_news")}
            </h2>
            <RecentTWNews />
          </div>
          <div>
            <h2 className="text-xs text-muted-foreground uppercase tracking-wider mb-2">
              {t("dashboard.latest_global_news")}
            </h2>
            <RecentGlobalNews />
          </div>
        </div>
      </div>
    </div>
  );
}
