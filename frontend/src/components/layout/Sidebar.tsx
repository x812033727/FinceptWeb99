import { useEffect } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  Activity,
  Bell,
  Bitcoin,
  BookOpen,
  Bot,
  Briefcase,
  ChevronDown,
  ChevronRight,
  Database,
  Globe,
  LayoutGrid,
  LineChart,
  Calculator,
  MessagesSquare,
  Search,
  Settings,
  Shield,
  Star,
  X,
  type LucideIcon,
} from "lucide-react";
import { logout } from "@/lib/auth";
import { useAuthStore } from "@/store/authStore";
import { useCollapsible } from "@/hooks/useCollapsible";
import { prefetchPage } from "@/pageLoaders";
import { cn } from "@/lib/utils";

interface NavItemDef {
  to: string;
  labelKey: string;
  Icon: LucideIcon;
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
      { to: "/dashboard", labelKey: "nav.dashboard", Icon: LayoutGrid },
      { to: "/market/US", labelKey: "nav.us_market", Icon: LineChart },
      { to: "/market/TW", labelKey: "nav.tw_market", Icon: Activity },
      { to: "/market/CRYPTO", labelKey: "nav.crypto", Icon: Bitcoin },
      { to: "/screener", labelKey: "nav.screener", Icon: Search },
      { to: "/macro", labelKey: "nav.macro", Icon: Globe },
    ],
  },
  {
    key: "workspace",
    labelKey: "nav.group.workspace",
    items: [
      { to: "/watchlist", labelKey: "nav.watchlist", Icon: Star },
      { to: "/alerts", labelKey: "nav.alerts", Icon: Bell },
      { to: "/portfolio", labelKey: "nav.portfolio", Icon: Briefcase },
      { to: "/analytics", labelKey: "nav.analytics", Icon: Calculator },
    ],
  },
  {
    key: "ai",
    labelKey: "nav.group.ai",
    items: [
      { to: "/ai", labelKey: "nav.ai", Icon: Bot },
      { to: "/discussion", labelKey: "nav.discussion", Icon: MessagesSquare },
      { to: "/discussion/lessons", labelKey: "nav.lesson_library", Icon: BookOpen },
      { to: "/discussion/compare", labelKey: "nav.strategy_compare", Icon: Activity },
    ],
  },
  {
    key: "data",
    labelKey: "nav.group.data",
    items: [{ to: "/finmind", labelKey: "nav.finmind", Icon: Database }],
  },
  {
    key: "system",
    labelKey: "nav.group.system",
    items: [
      { to: "/settings", labelKey: "nav.settings", Icon: Settings },
      { to: "/admin", labelKey: "nav.admin", Icon: Shield, adminOnly: true },
    ],
  },
];

function NavItem({ item, onNavigate }: { item: NavItemDef; onNavigate?: () => void }) {
  const { t } = useTranslation();
  const prefetch = () => prefetchPage(item.to);
  const Icon = item.Icon;
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
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
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
        {open ? (
          <ChevronDown className="h-3 w-3" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3 w-3" aria-hidden="true" />
        )}
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
            className="lg:hidden p-1 rounded text-muted-foreground hover:text-foreground hover:bg-accent/10 min-h-[32px] min-w-[32px] flex items-center justify-center"
          >
            <X className="h-4 w-4" aria-hidden="true" />
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
