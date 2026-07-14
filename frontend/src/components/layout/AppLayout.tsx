import { useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Bell, Globe, Menu, MoreHorizontal, Sun, Moon, X } from "lucide-react";
import Sidebar from "./Sidebar";
import GlobalSearch from "./GlobalSearch";
import UpdateBadge from "./UpdateBadge";
import BottomNav from "./BottomNav";
import { useAlertSocket, useWsConnected } from "@/hooks/useWebSocket";
import { useNotificationStore } from "@/store/notificationStore";
import { useThemeStore } from "@/store/themeStore";
import { useCommandPalette } from "@/hooks/useCommandPalette";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

function WsStatus({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation();
  const connected = useWsConnected();
  return (
    <div
      className={`flex items-center gap-1.5 text-micro text-muted-foreground select-none ${
        compact ? "" : ""
      }`}
      title={connected ? t("topbar.ws_connected") : t("topbar.ws_disconnected")}
    >
      <span
        className={`w-2 h-2 rounded-full shrink-0 ${
          connected ? "bg-success" : "bg-danger animate-pulse"
        }`}
      />
      {!compact && (
        <span className="hidden sm:inline">{connected ? t("topbar.live") : t("topbar.off")}</span>
      )}
    </div>
  );
}

function NotificationBell() {
  const { t } = useTranslation();
  const { alerts, unreadCount, markAllRead, dismiss } = useNotificationStore();
  const [open, setOpen] = useState(false);

  useAlertSocket((alert) => {
    useNotificationStore.getState().addAlert(alert);
  });

  function handleOpenChange(next: boolean) {
    setOpen(next);
    if (next) markAllRead();
  }

  return (
    <DropdownMenu open={open} onOpenChange={handleOpenChange}>
      <DropdownMenuTrigger
        title={t("topbar.alerts_title")}
        aria-label={t("topbar.alerts_title")}
        className="relative p-1.5 rounded hover:bg-accent/10 text-muted-foreground hover:text-foreground transition-colors min-h-[36px] min-w-[36px] flex items-center justify-center"
      >
        <Bell className="h-4 w-4" aria-hidden="true" />
        {unreadCount > 0 && (
          <span className="absolute top-0.5 right-0.5 min-w-[16px] h-4 flex items-center justify-center text-micro font-bold rounded-full bg-primary text-primary-foreground px-0.5">
            {unreadCount > 9 ? "9+" : unreadCount}
          </span>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="w-80 max-w-[calc(100vw-1.5rem)] p-0 overflow-hidden"
      >
        <DropdownMenuLabel className="border-b border-border text-xs font-medium normal-case tracking-normal text-foreground">
          {t("topbar.price_alerts")}
        </DropdownMenuLabel>
        {alerts.length === 0 ? (
          <p className="px-3 py-4 text-xs text-muted-foreground text-center">
            {t("topbar.no_alerts")}
          </p>
        ) : (
          <ul className="max-h-72 overflow-y-auto divide-y divide-border">
            {alerts.map((a) => (
              <li
                key={a.id}
                className="flex items-start justify-between gap-2 px-3 py-2.5"
              >
                <div className="text-xs space-y-0.5">
                  <span className="font-medium">
                    {a.symbol} ({a.market})
                  </span>
                  <p className="text-muted-foreground">
                    {a.condition === "above"
                      ? t("topbar.price_above")
                      : t("topbar.price_below")}{" "}
                    <span
                      className={
                        a.condition === "above" ? "text-up" : "text-down"
                      }
                    >
                      {a.target_price.toFixed(2)}
                    </span>{" "}
                    — {t("topbar.hit")}{" "}
                    <span className="text-foreground">
                      {a.current_price.toFixed(2)}
                    </span>
                  </p>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    dismiss(a.id);
                  }}
                  aria-label={t("common.remove")}
                  className="text-muted-foreground hover:text-foreground shrink-0 min-h-[32px] min-w-[32px] flex items-center justify-center rounded hover:bg-accent/10"
                >
                  <X className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ThemeToggle() {
  const { t } = useTranslation();
  const { theme, toggle } = useThemeStore();
  const Icon = theme === "dark" ? Sun : Moon;
  return (
    <button
      onClick={toggle}
      className="p-1.5 rounded hover:bg-accent/10 text-muted-foreground hover:text-foreground transition-colors min-h-[36px] min-w-[36px] flex items-center justify-center"
      title={theme === "dark" ? t("topbar.switch_to_light") : t("topbar.switch_to_dark")}
      aria-label={theme === "dark" ? t("topbar.switch_to_light") : t("topbar.switch_to_dark")}
    >
      <Icon className="h-4 w-4" aria-hidden="true" />
    </button>
  );
}

function LanguageToggle() {
  const { i18n, t } = useTranslation();
  const current = i18n.language;
  const next = current === "zh-TW" ? "en" : "zh-TW";
  const label = current === "zh-TW" ? "EN" : "中";
  return (
    <button
      onClick={() => void i18n.changeLanguage(next)}
      className="px-2 rounded text-[11px] font-medium hover:bg-accent/10 text-muted-foreground hover:text-foreground transition-colors min-h-[36px] min-w-[36px] flex items-center justify-center"
      title={t("topbar.language")}
    >
      {label}
    </button>
  );
}

function MenuButton({ onOpen }: { onOpen: () => void }) {
  const { t } = useTranslation();
  return (
    <button
      onClick={onOpen}
      aria-label={t("topbar.menu")}
      className="lg:hidden p-1.5 -ml-1 rounded text-muted-foreground hover:text-foreground hover:bg-accent/10 transition-colors min-h-[36px] min-w-[36px] flex items-center justify-center"
    >
      <Menu className="h-5 w-5" aria-hidden="true" />
    </button>
  );
}

// Compact "more" menu surfacing the lower-frequency topbar controls
// (theme / language / WS status / update badge) on narrow viewports
// where every pixel of topbar space matters.
function MoreMenu() {
  const { t, i18n } = useTranslation();
  const { theme, toggle: toggleTheme } = useThemeStore();
  const connected = useWsConnected();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={t("topbar.more")}
        className="lg:hidden p-1.5 rounded hover:bg-accent/10 text-muted-foreground hover:text-foreground transition-colors min-h-[36px] min-w-[36px] flex items-center justify-center"
      >
        <MoreHorizontal className="h-4 w-4" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-52">
        <DropdownMenuLabel>{t("topbar.preferences")}</DropdownMenuLabel>
        <DropdownMenuItem onSelect={toggleTheme}>
          {theme === "dark" ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
          {theme === "dark" ? t("topbar.switch_to_light") : t("topbar.switch_to_dark")}
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => {
            const next = i18n.language === "zh-TW" ? "en" : "zh-TW";
            void i18n.changeLanguage(next);
          }}
        >
          <Globe className="h-3.5 w-3.5" />
          {i18n.language === "zh-TW" ? "English" : "中文"}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel>{t("topbar.status")}</DropdownMenuLabel>
        <div className="px-2 py-1.5 flex items-center gap-2 text-xs">
          <span
            className={`w-2 h-2 rounded-full shrink-0 ${
              connected ? "bg-success" : "bg-danger"
            }`}
          />
          <span className="text-muted-foreground">
            {connected ? t("topbar.ws_connected") : t("topbar.ws_disconnected")}
          </span>
        </div>
        <div className="px-2 py-1.5">
          <UpdateBadge />
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function PaletteHint() {
  const { t } = useTranslation();
  const { setOpen } = useCommandPalette();
  return (
    <button
      type="button"
      onClick={() => setOpen(true)}
      className="hidden lg:inline-flex items-center gap-1.5 px-2 py-1 rounded border border-border text-micro text-muted-foreground hover:text-foreground hover:bg-accent/10 transition-colors"
      title={t("palette.title")}
    >
      <kbd className="font-sans">⌘</kbd>
      <kbd className="font-sans">K</kbd>
    </button>
  );
}

export default function AppLayout() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const location = useLocation();

  // Auto-close drawer on route change. NavLink onClick already calls
  // onClose, but this also covers programmatic navigation (e.g. login
  // redirect, search-bar selections).
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSidebarOpen(false);
  }, [location.pathname]);

  // Lock body scroll while drawer is open on mobile so the page behind
  // doesn't scroll under the user's thumb.
  useEffect(() => {
    if (sidebarOpen) {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => {
        document.body.style.overflow = prev;
      };
    }
  }, [sidebarOpen]);

  return (
    <div className="flex min-h-screen bg-background">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <div className="h-11 border-b border-border-subtle shadow-highlight flex items-center gap-2 sm:gap-3 px-3 sm:px-4 shrink-0">
          <MenuButton onOpen={() => setSidebarOpen(true)} />
          <GlobalSearch />
          <PaletteHint />
          <div className="flex items-center gap-1 sm:gap-2 ml-auto">
            <span className="hidden lg:inline-block">
              <UpdateBadge />
            </span>
            <span className="hidden lg:inline-flex">
              <WsStatus />
            </span>
            <span className="hidden lg:inline-flex">
              <LanguageToggle />
            </span>
            <span className="hidden lg:inline-flex">
              <ThemeToggle />
            </span>
            <NotificationBell />
            <MoreMenu />
          </div>
        </div>
        <main className="flex-1 overflow-auto pb-[calc(env(safe-area-inset-bottom)+var(--nav-h))] lg:pb-0">
          <Outlet />
        </main>
      </div>
      <BottomNav onOpenMore={() => setSidebarOpen(true)} />
    </div>
  );
}
