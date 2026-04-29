import { NavLink } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { logout } from "@/lib/auth";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/store/authStore";
import { prefetchPage } from "@/pageLoaders";

interface NavItemDef {
  to: string;
  labelKey: string;
  icon: string;
}

const NAV: NavItemDef[] = [
  { to: "/dashboard", labelKey: "nav.dashboard", icon: "⊞" },
  { to: "/market/US", labelKey: "nav.us_market", icon: "🇺🇸" },
  { to: "/market/TW", labelKey: "nav.tw_market", icon: "🇹🇼" },
  { to: "/market/CRYPTO", labelKey: "nav.crypto", icon: "₿" },
  { to: "/screener", labelKey: "nav.screener", icon: "🔍" },
  { to: "/watchlist", labelKey: "nav.watchlist", icon: "⭐" },
  { to: "/alerts", labelKey: "nav.alerts", icon: "🔔" },
  { to: "/portfolio", labelKey: "nav.portfolio", icon: "📊" },
  { to: "/analytics", labelKey: "nav.analytics", icon: "📐" },
  { to: "/macro", labelKey: "nav.macro", icon: "🌐" },
  { to: "/ai", labelKey: "nav.ai", icon: "🤖" },
  { to: "/discussion", labelKey: "nav.discussion", icon: "💬" },
  { to: "/settings", labelKey: "nav.settings", icon: "⚙" },
];

function NavItem({ item, onNavigate }: { item: NavItemDef; onNavigate?: () => void }) {
  const { t } = useTranslation();
  // Warm the destination page's chunk on hover (desktop) or touch
  // (mobile) so by the time the click handler fires React.lazy already
  // has the module — no Suspense fallback flicker. Prefetch is
  // idempotent + cached, so spamming hovers is free.
  const prefetch = () => prefetchPage(item.to);
  return (
    <NavLink
      to={item.to}
      onClick={onNavigate}
      onMouseEnter={prefetch}
      onFocus={prefetch}
      onTouchStart={prefetch}
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

interface SidebarProps {
  /** Mobile drawer open-state. Ignored on `lg:` and up where the
   *  sidebar is permanently visible. */
  open: boolean;
  onClose: () => void;
}

export default function Sidebar({ open, onClose }: SidebarProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const role = useAuthStore((s) => s.user?.role);

  async function handleLogout() {
    await logout();
    onClose();
    navigate("/login");
  }

  return (
    <>
      {/* Backdrop — mobile only, fades with the drawer. pointer-events-none
          when closed so it doesn't block underlying content. */}
      <div
        onClick={onClose}
        aria-hidden="true"
        className={`fixed inset-0 bg-black/60 z-40 lg:hidden transition-opacity duration-200 ${
          open ? "opacity-100" : "opacity-0 pointer-events-none"
        }`}
      />

      <aside
        className={`fixed lg:sticky top-0 left-0 z-50 h-screen w-64 lg:w-44 shrink-0 border-r border-border bg-card flex flex-col transform transition-transform duration-200 lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full lg:translate-x-0"
        }`}
      >
        {/* branding */}
        <div className="px-4 py-4 border-b border-border flex items-center justify-between">
          <div>
            <span className="text-primary font-bold text-sm tracking-wider">FINCEPT</span>
            <span className="text-muted-foreground text-xs ml-1">WEB</span>
          </div>
          <button
            onClick={onClose}
            aria-label={t("topbar.close_menu")}
            className="lg:hidden p-1 rounded text-muted-foreground hover:text-foreground hover:bg-accent/10 text-base leading-none"
          >
            ×
          </button>
        </div>

        {/* nav */}
        <nav className="flex-1 overflow-y-auto py-3 space-y-0.5 px-2">
          {NAV.map((item) => (
            <NavItem key={item.to} item={item} onNavigate={onClose} />
          ))}
          {role === "admin" && (
            <NavItem
              item={{ to: "/admin", labelKey: "nav.admin", icon: "🛡" }}
              onNavigate={onClose}
            />
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
    </>
  );
}
