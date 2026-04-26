import { NavLink } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { logout } from "@/lib/auth";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/store/authStore";

interface NavItemDef {
  to: string;
  labelKey: string;
  icon: string;
}

const NAV: NavItemDef[] = [
  { to: "/dashboard", labelKey: "nav.dashboard", icon: "⊞" },
  { to: "/market/US", labelKey: "nav.us_market", icon: "🇺🇸" },
  { to: "/market/TW", labelKey: "nav.tw_market", icon: "🇹🇼" },
  { to: "/screener", labelKey: "nav.screener", icon: "🔍" },
  { to: "/watchlist", labelKey: "nav.watchlist", icon: "⭐" },
  { to: "/alerts", labelKey: "nav.alerts", icon: "🔔" },
  { to: "/portfolio", labelKey: "nav.portfolio", icon: "📊" },
  { to: "/analytics", labelKey: "nav.analytics", icon: "📐" },
  { to: "/macro", labelKey: "nav.macro", icon: "🌐" },
  { to: "/ai", labelKey: "nav.ai", icon: "🤖" },
  { to: "/settings", labelKey: "nav.settings", icon: "⚙" },
];

function NavItem({ item }: { item: NavItemDef }) {
  const { t } = useTranslation();
  return (
    <NavLink
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
      {t(item.labelKey)}
    </NavLink>
  );
}

export default function Sidebar() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const role = useAuthStore((s) => s.user?.role);

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
          <NavItem key={item.to} item={item} />
        ))}
        {role === "admin" && (
          <NavItem item={{ to: "/admin", labelKey: "nav.admin", icon: "🛡" }} />
        )}
      </nav>

      {/* footer */}
      <div className="border-t border-border p-3">
        <button
          onClick={handleLogout}
          className="w-full text-left px-3 py-2 rounded-md text-xs text-muted-foreground hover:text-foreground hover:bg-accent/10 transition-colors"
        >
          {t("nav.sign_out")}
        </button>
      </div>
    </aside>
  );
}
