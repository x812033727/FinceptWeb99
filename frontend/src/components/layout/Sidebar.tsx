import { useEffect } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { logout } from "@/lib/auth";
import { useAuthStore } from "@/store/authStore";
import { useCollapsible } from "@/components/Collapsible";
import { prefetchPage } from "@/pageLoaders";
import { cn } from "@/lib/utils";

interface NavItemDef {
  to: string;
  labelKey: string;
  icon: string;
  adminOnly?: boolean;
}

interface NavGroupDef {
  key: string;
  labelKey: string;
  items: NavItemDef[];
}

const NAV_GROUPS: NavGroupDef[] = [
  {
    key: "markets",
    labelKey: "nav.group.markets",
    items: [
      { to: "/dashboard", labelKey: "nav.dashboard", icon: "⊞" },
      { to: "/market/US", labelKey: "nav.us_market", icon: "🇺🇸" },
      { to: "/market/TW", labelKey: "nav.tw_market", icon: "🇹🇼" },
      { to: "/market/CRYPTO", labelKey: "nav.crypto", icon: "₿" },
      { to: "/screener", labelKey: "nav.screener", icon: "🔍" },
      { to: "/macro", labelKey: "nav.macro", icon: "🌐" },
    ],
  },
  {
    key: "workspace",
    labelKey: "nav.group.workspace",
    items: [
      { to: "/watchlist", labelKey: "nav.watchlist", icon: "⭐" },
      { to: "/alerts", labelKey: "nav.alerts", icon: "🔔" },
      { to: "/portfolio", labelKey: "nav.portfolio", icon: "📊" },
      { to: "/analytics", labelKey: "nav.analytics", icon: "📐" },
    ],
  },
  {
    key: "ai",
    labelKey: "nav.group.ai",
    items: [
      { to: "/ai", labelKey: "nav.ai", icon: "🤖" },
      { to: "/discussion", labelKey: "nav.discussion", icon: "💬" },
    ],
  },
  {
    key: "data",
    labelKey: "nav.group.data",
    items: [{ to: "/finmind", labelKey: "nav.finmind", icon: "🧬" }],
  },
  {
    key: "system",
    labelKey: "nav.group.system",
    items: [
      { to: "/settings", labelKey: "nav.settings", icon: "⚙" },
      { to: "/admin", labelKey: "nav.admin", icon: "🛡", adminOnly: true },
    ],
  },
];

function NavItem({ item, onNavigate }: { item: NavItemDef; onNavigate?: () => void }) {
  const { t } = useTranslation();
  const prefetch = () => prefetchPage(item.to);
  return (
    <NavLink
      to={item.to}
      onClick={onNavigate}
      onMouseEnter={prefetch}
      onFocus={prefetch}
      onTouchStart={prefetch}
      className={({ isActive }) =>
        cn(
          "flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors min-h-[36px]",
          isActive
            ? "bg-primary/15 text-primary font-medium"
            : "text-muted-foreground hover:text-foreground hover:bg-accent/10"
        )
      }
    >
      <span className="text-base leading-none w-4">{item.icon}</span>
      {t(item.labelKey)}
    </NavLink>
  );
}

interface NavGroupProps {
  group: NavGroupDef;
  visibleItems: NavItemDef[];
  defaultOpen: boolean;
  onNavigate?: () => void;
}

function NavGroup({ group, visibleItems, defaultOpen, onNavigate }: NavGroupProps) {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible(`sidebar.group.${group.key}`, defaultOpen);
  return (
    <div className="space-y-0.5">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="w-full flex items-center justify-between px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground transition-colors min-h-[28px]"
      >
        <span>{t(group.labelKey)}</span>
        <span className="text-[9px]" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <div className="space-y-0.5">
          {visibleItems.map((item) => (
            <NavItem key={item.to} item={item} onNavigate={onNavigate} />
          ))}
        </div>
      )}
    </div>
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
  const location = useLocation();
  const role = useAuthStore((s) => s.user?.role);

  // Pin all destinations the user might tap so the chunk is warm by the
  // time the drawer animation finishes. Cheap (cached) once fired.
  useEffect(() => {
    if (!open) return;
    NAV_GROUPS.forEach((g) => g.items.forEach((i) => prefetchPage(i.to)));
  }, [open]);

  async function handleLogout() {
    await logout();
    onClose();
    navigate("/login");
  }

  const groupContaining = (path: string): string | null => {
    for (const g of NAV_GROUPS) {
      if (g.items.some((i) => path === i.to || path.startsWith(`${i.to}/`))) {
        return g.key;
      }
    }
    return null;
  };
  const currentGroupKey = groupContaining(location.pathname);

  return (
    <>
      <div
        onClick={onClose}
        aria-hidden="true"
        className={cn(
          "fixed inset-0 bg-black/60 z-40 lg:hidden transition-opacity duration-200",
          open ? "opacity-100" : "opacity-0 pointer-events-none"
        )}
      />

      <aside
        className={cn(
          "fixed lg:sticky top-0 left-0 z-50 h-screen w-64 lg:w-48 shrink-0 border-r border-border bg-card flex flex-col transform transition-transform duration-200 lg:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full lg:translate-x-0"
        )}
      >
        <div className="px-4 py-4 border-b border-border flex items-center justify-between">
          <div>
            <span className="text-primary font-bold text-sm tracking-wider">FINCEPT</span>
            <span className="text-muted-foreground text-xs ml-1">WEB</span>
          </div>
          <button
            onClick={onClose}
            aria-label={t("topbar.close_menu")}
            className="lg:hidden p-1 rounded text-muted-foreground hover:text-foreground hover:bg-accent/10 text-base leading-none min-h-[32px] min-w-[32px]"
          >
            ×
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto py-3 space-y-3 px-2">
          {NAV_GROUPS.map((group) => {
            const visibleItems = group.items.filter((i) => !i.adminOnly || role === "admin");
            if (visibleItems.length === 0) return null;
            return (
              <NavGroup
                key={group.key}
                group={group}
                visibleItems={visibleItems}
                defaultOpen={group.key === currentGroupKey || group.key === "markets"}
                onNavigate={onClose}
              />
            );
          })}
        </nav>

        <div className="border-t border-border p-3">
          <button
            onClick={handleLogout}
            className="w-full text-left px-3 py-2 rounded-md text-xs text-muted-foreground hover:text-foreground hover:bg-accent/10 transition-colors min-h-[36px]"
          >
            {t("nav.sign_out")}
          </button>
        </div>
      </aside>
    </>
  );
}
