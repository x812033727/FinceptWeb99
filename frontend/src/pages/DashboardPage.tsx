import { useAuthStore } from "@/store/authStore";
import { Link } from "react-router-dom";

const CARDS = [
  { label: "US Market", desc: "S&P 500 quotes & screener", icon: "🇺🇸", href: "/market/US" },
  { label: "台股", desc: "上市上櫃即時行情", icon: "🇹🇼", href: "/market/TW" },
  { label: "Screener", desc: "Filter stocks by any metric", icon: "🔍", href: "/screener" },
  { label: "Portfolio", desc: "Holdings, P&L & optimizer", icon: "📊", href: "/portfolio" },
  { label: "Analytics", desc: "DCF · VaR · Backtest", icon: "📐", href: "/analytics" },
  { label: "AI Agents", desc: "6 CFA-grade personas", icon: "🤖", href: "/ai" },
];

export default function DashboardPage() {
  const { user } = useAuthStore();

  return (
    <div className="p-8 space-y-8">
      {/* welcome */}
      <div>
        <h1 className="text-2xl font-bold text-foreground">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Welcome back, <span className="text-foreground">{user?.email}</span>
          {" · "}
          <span className="capitalize text-primary">{user?.role}</span>
        </p>
      </div>

      {/* nav cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4">
        {CARDS.map((card) => (
          <Link
            key={card.label}
            to={card.href}
            className="bg-card border border-border rounded-lg p-5 space-y-2 hover:border-primary/50 hover:bg-card/80 transition-colors block group"
          >
            <div className="text-2xl">{card.icon}</div>
            <h2 className="text-foreground font-medium group-hover:text-primary transition-colors">
              {card.label}
            </h2>
            <p className="text-xs text-muted-foreground">{card.desc}</p>
          </Link>
        ))}
      </div>

      {/* quota */}
      {user?.ai_requests_remaining !== undefined && (
        <div className="text-xs text-muted-foreground border-t border-border pt-4">
          AI requests remaining today:{" "}
          <span className="text-foreground font-medium">{user.ai_requests_remaining}</span>
        </div>
      )}
    </div>
  );
}
