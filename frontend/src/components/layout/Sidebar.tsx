import { NavLink } from "react-router-dom";
import { logout } from "@/lib/auth";
import { useNavigate } from "react-router-dom";

interface NavItem {
  to: string;
  label: string;
  icon: string;
}

const NAV: NavItem[] = [
  { to: "/dashboard", label: "Dashboard", icon: "⊞" },
  { to: "/market/US", label: "US Market", icon: "🇺🇸" },
  { to: "/market/TW", label: "台股", icon: "🇹🇼" },
  { to: "/screener", label: "Screener", icon: "🔍" },
  { to: "/watchlist", label: "Watchlist", icon: "⭐" },
  { to: "/portfolio", label: "Portfolio", icon: "📊" },
  { to: "/analytics", label: "Analytics", icon: "📐" },
  { to: "/macro", label: "Macro", icon: "🌐" },
  { to: "/ai", label: "AI Agents", icon: "🤖" },
];

export default function Sidebar() {
  const navigate = useNavigate();

  async function handleLogout() {
    await logout();
    navigate("/login");
  }

  return (
    <aside className="w-52 shrink-0 border-r border-border bg-card flex flex-col h-screen sticky top-0">
      {/* branding */}
      <div className="px-4 py-4 border-b border-border">
        <span className="text-primary font-bold text-sm tracking-wider">FINCEPT</span>
        <span className="text-muted-foreground text-xs ml-1">WEB</span>
      </div>

      {/* nav */}
      <nav className="flex-1 overflow-y-auto py-3 space-y-0.5 px-2">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) =>
              `flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors ${
                isActive
                  ? "bg-primary/15 text-primary font-medium"
                  : "text-muted-foreground hover:text-foreground hover:bg-accent/10"
              }`
            }
          >
            <span className="text-base leading-none">{item.icon}</span>
            {item.label}
          </NavLink>
        ))}
      </nav>

      {/* footer */}
      <div className="border-t border-border p-3">
        <button
          onClick={handleLogout}
          className="w-full text-left px-3 py-2 rounded-md text-xs text-muted-foreground hover:text-foreground hover:bg-accent/10 transition-colors"
        >
          Sign out
        </button>
      </div>
    </aside>
  );
}
