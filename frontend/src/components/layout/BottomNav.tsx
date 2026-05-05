import { useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { LayoutGrid, LineChart, Briefcase, Bot, Menu } from "lucide-react";
import { prefetchPage } from "@/pageLoaders";
import { cn } from "@/lib/utils";

interface BottomNavProps {
  onOpenMore: () => void;
}

interface TabDef {
  to: string;
  labelKey: string;
  Icon: typeof LayoutGrid;
  /** Predicate for "this tab is active" given the current pathname. */
  matches: (pathname: string) => boolean;
}

const TABS: TabDef[] = [
  {
    to: "/dashboard",
    labelKey: "bottom_nav.dashboard",
    Icon: LayoutGrid,
    matches: (p) => p === "/" || p.startsWith("/dashboard"),
  },
  {
    to: "/market/US",
    labelKey: "bottom_nav.markets",
    Icon: LineChart,
    // Catches /market/* and /stock/* and /screener so the user always
    // sees a stable "you're in markets" affordance while drilling in.
    matches: (p) => p.startsWith("/market") || p.startsWith("/stock") || p.startsWith("/screener"),
  },
  {
    to: "/portfolio",
    labelKey: "bottom_nav.portfolio",
    Icon: Briefcase,
    matches: (p) =>
      p.startsWith("/portfolio") || p.startsWith("/watchlist") || p.startsWith("/alerts"),
  },
  {
    to: "/ai",
    labelKey: "bottom_nav.ai",
    Icon: Bot,
    matches: (p) => p.startsWith("/ai") || p.startsWith("/discussion"),
  },
];

export default function BottomNav({ onOpenMore }: BottomNavProps) {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();

  return (
    <nav
      className="fixed bottom-0 inset-x-0 z-30 lg:hidden border-t border-border bg-card/95 backdrop-blur"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      aria-label={t("bottom_nav.aria")}
    >
      <ul className="flex items-stretch h-14">
        {TABS.map(({ to, labelKey, Icon, matches }) => {
          const active = matches(location.pathname);
          return (
            <li key={to} className="flex-1">
              <button
                type="button"
                onClick={() => navigate(to)}
                onTouchStart={() => prefetchPage(to)}
                className={cn(
                  "w-full h-full flex flex-col items-center justify-center gap-0.5 min-h-[44px]",
                  active ? "text-primary" : "text-muted-foreground hover:text-foreground"
                )}
                aria-current={active ? "page" : undefined}
              >
                <Icon className="h-5 w-5" aria-hidden="true" />
                <span className="text-[10px] font-medium leading-none">{t(labelKey)}</span>
              </button>
            </li>
          );
        })}
        <li className="flex-1">
          <button
            type="button"
            onClick={onOpenMore}
            className="w-full h-full flex flex-col items-center justify-center gap-0.5 min-h-[44px] text-muted-foreground hover:text-foreground"
            aria-label={t("bottom_nav.more")}
          >
            <Menu className="h-5 w-5" aria-hidden="true" />
            <span className="text-[10px] font-medium leading-none">{t("bottom_nav.more")}</span>
          </button>
        </li>
      </ul>
    </nav>
  );
}
