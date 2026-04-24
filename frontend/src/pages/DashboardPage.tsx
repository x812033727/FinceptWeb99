import { useAuthStore } from "@/store/authStore";
import { logout } from "@/lib/auth";
import { useNavigate, Link } from "react-router-dom";

export default function DashboardPage() {
  const { user } = useAuthStore();
  const navigate = useNavigate();

  async function handleLogout() {
    await logout();
    navigate("/login");
  }

  return (
    <div className="min-h-screen bg-background p-8">
      <div className="max-w-4xl mx-auto space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-primary">Dashboard</h1>
            <p className="text-sm text-muted-foreground mt-1">
              Welcome, <span className="text-foreground">{user?.email}</span>
              {" · "}
              <span className="capitalize text-accent">{user?.role}</span>
            </p>
          </div>
          <button
            onClick={handleLogout}
            className="px-4 py-2 text-sm rounded-md border border-border text-muted-foreground hover:text-foreground hover:border-foreground transition-colors"
          >
            Sign out
          </button>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          {[
            { label: "US Market", desc: "Coming in Phase 9", icon: "🇺🇸", href: null },
            { label: "台股", desc: "Coming in Phase 9", icon: "🇹🇼", href: null },
            { label: "Portfolio", desc: "Track holdings & P&L", icon: "📊", href: "/portfolio" },
            { label: "Analytics", desc: "DCF · VaR · Backtest", icon: "📐", href: "/analytics" },
          ].map((card) => (
            card.href
              ? <Link key={card.label} to={card.href} className="bg-card border border-border rounded-lg p-5 space-y-2 hover:border-primary/50 transition-colors block">
                  <div className="text-2xl">{card.icon}</div>
                  <h2 className="text-foreground font-medium">{card.label}</h2>
                  <p className="text-xs text-muted-foreground">{card.desc}</p>
                </Link>
              : <div key={card.label} className="bg-card border border-border rounded-lg p-5 space-y-2 opacity-60">
                  <div className="text-2xl">{card.icon}</div>
                  <h2 className="text-foreground font-medium">{card.label}</h2>
                  <p className="text-xs text-muted-foreground">{card.desc}</p>
                </div>
          ))}
        </div>

        {user?.ai_requests_remaining !== undefined && (
          <p className="text-xs text-muted-foreground">
            AI requests remaining today: {user.ai_requests_remaining}
          </p>
        )}
      </div>
    </div>
  );
}
