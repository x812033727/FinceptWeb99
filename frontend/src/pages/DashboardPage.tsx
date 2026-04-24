import { useAuthStore } from "@/store/authStore";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";

// ── Market indices ─────────────────────────────────────────────────

const INDICES = [
  { symbol: "SPY",  label: "S&P 500" },
  { symbol: "QQQ",  label: "NASDAQ 100" },
  { symbol: "IWM",  label: "Russell 2000" },
  { symbol: "GLD",  label: "Gold ETF" },
];

function IndexCard({ symbol, label }: { symbol: string; label: string }) {
  const { data } = useQuery({
    queryKey: ["quote", "US", symbol],
    queryFn: () => api.get<Record<string, unknown>>(`/us/quote/${symbol}`).then((r) => r.data),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });

  const price = data?.price as number | undefined;
  const changePct = data?.change_pct as number | undefined;
  const isPos = (changePct ?? 0) >= 0;

  return (
    <Link
      to={`/stock/US/${symbol}`}
      className="bg-card border border-border rounded-lg p-4 hover:border-primary/40 transition-colors"
    >
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <div className="text-lg font-bold text-foreground">
        {price != null ? price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—"}
      </div>
      <div className={`text-sm font-medium mt-0.5 ${isPos ? "text-green-400" : "text-red-400"}`}>
        {changePct != null ? `${isPos ? "+" : ""}${changePct.toFixed(2)}%` : "—"}
      </div>
      <div className="text-xs text-muted-foreground mt-1">{symbol}</div>
    </Link>
  );
}

// ── Nav cards ─────────────────────────────────────────────────────

const CARDS = [
  { label: "US Market",  desc: "S&P 500 quotes & screener",  icon: "🇺🇸", href: "/market/US" },
  { label: "台股",        desc: "上市上櫃即時行情",             icon: "🇹🇼", href: "/market/TW" },
  { label: "Screener",   desc: "Filter stocks by any metric", icon: "🔍", href: "/screener" },
  { label: "Watchlist",  desc: "Track symbols with live prices", icon: "⭐", href: "/watchlist" },
  { label: "Portfolio",  desc: "Holdings, P&L & optimizer",   icon: "📊", href: "/portfolio" },
  { label: "Analytics",  desc: "DCF · VaR · Backtest",        icon: "📐", href: "/analytics" },
  { label: "Macro",      desc: "FRED indicators · yield curve", icon: "🌐", href: "/macro" },
  { label: "AI Agents",  desc: "6 CFA-grade personas",        icon: "🤖", href: "/ai" },
];

// ── Recent news ────────────────────────────────────────────────────

interface NewsItem {
  title: string;
  publisher: string;
  link: string;
  published_at: string;
}

function RecentNews() {
  const { data: items = [], isLoading } = useQuery<NewsItem[]>({
    queryKey: ["news", "US", "SPY"],
    queryFn: () => api.get("/api/us/news/SPY").then((r) => r.data),
    staleTime: 5 * 60_000,
  });

  if (isLoading) {
    return <div className="text-xs text-muted-foreground animate-pulse">Loading news…</div>;
  }

  if (!items.length) return null;

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h2 className="text-sm font-medium text-foreground">Market News</h2>
      </div>
      <div className="divide-y divide-border/50">
        {items.slice(0, 5).map((item, i) => (
          <a
            key={i}
            href={item.link}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-start gap-3 px-4 py-3 hover:bg-accent/5 transition-colors"
          >
            <div className="flex-1 min-w-0">
              <p className="text-sm text-foreground leading-snug line-clamp-2">{item.title}</p>
              <p className="text-xs text-muted-foreground mt-1">
                {item.publisher} ·{" "}
                {new Date(item.published_at).toLocaleDateString(undefined, {
                  month: "short", day: "numeric",
                })}
              </p>
            </div>
          </a>
        ))}
      </div>
    </div>
  );
}

// ── Main ──────────────────────────────────────────────────────────

export default function DashboardPage() {
  const { user } = useAuthStore();

  return (
    <div className="p-6 space-y-6">
      {/* welcome */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">Dashboard</h1>
          <p className="text-sm text-muted-foreground mt-0.5">
            Welcome back, <span className="text-foreground">{user?.email}</span>
            {" · "}
            <span className="capitalize text-primary">{user?.role}</span>
          </p>
        </div>
        {user?.ai_requests_remaining !== undefined && (
          <div className="text-xs text-muted-foreground text-right">
            AI requests today
            <div className="text-foreground font-medium text-sm">{user.ai_requests_remaining} remaining</div>
          </div>
        )}
      </div>

      {/* market indices */}
      <div>
        <h2 className="text-xs text-muted-foreground uppercase tracking-wider mb-2">Market Overview</h2>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {INDICES.map((idx) => (
            <IndexCard key={idx.symbol} symbol={idx.symbol} label={idx.label} />
          ))}
        </div>
      </div>

      {/* nav cards + news */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <h2 className="text-xs text-muted-foreground uppercase tracking-wider mb-2">Quick Access</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {CARDS.map((card) => (
              <Link
                key={card.label}
                to={card.href}
                className="bg-card border border-border rounded-lg p-4 space-y-1.5 hover:border-primary/50 hover:bg-card/80 transition-colors block group"
              >
                <div className="text-xl">{card.icon}</div>
                <h2 className="text-foreground font-medium text-sm group-hover:text-primary transition-colors">
                  {card.label}
                </h2>
                <p className="text-xs text-muted-foreground">{card.desc}</p>
              </Link>
            ))}
          </div>
        </div>

        <div>
          <h2 className="text-xs text-muted-foreground uppercase tracking-wider mb-2">Latest News</h2>
          <RecentNews />
        </div>
      </div>
    </div>
  );
}
