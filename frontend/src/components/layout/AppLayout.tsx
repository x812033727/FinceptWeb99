import { useEffect, useRef, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import Sidebar from "./Sidebar";
import GlobalSearch from "./GlobalSearch";
import UpdateBadge from "./UpdateBadge";
import { useAlertSocket, useWsConnected } from "@/hooks/useWebSocket";
import { useNotificationStore } from "@/store/notificationStore";
import { useThemeStore } from "@/store/themeStore";

function WsStatus() {
  const { t } = useTranslation();
  const connected = useWsConnected();
  return (
    <div
      className="flex items-center gap-1.5 text-[10px] text-muted-foreground select-none"
      title={connected ? t("topbar.ws_connected") : t("topbar.ws_disconnected")}
    >
      <span
        className={`w-2 h-2 rounded-full shrink-0 ${
          connected ? "bg-green-500" : "bg-red-500 animate-pulse"
        }`}
      />
      <span className="hidden sm:inline">{connected ? t("topbar.live") : t("topbar.off")}</span>
    </div>
  );
}

function NotificationBell() {
  const { t } = useTranslation();
  const { alerts, unreadCount, markAllRead, dismiss } = useNotificationStore();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useAlertSocket((alert) => {
    useNotificationStore.getState().addAlert(alert);
  });

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  function toggle() {
    setOpen((v) => {
      if (!v) markAllRead();
      return !v;
    });
  }

  return (
    <div ref={ref} className="relative">
      <button
        onClick={toggle}
        className="relative p-1.5 rounded hover:bg-accent/10 text-muted-foreground hover:text-foreground transition-colors"
        title={t("topbar.alerts_title")}
      >
        <span className="text-base leading-none">🔔</span>
        {unreadCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 flex items-center justify-center text-[10px] font-bold rounded-full bg-primary text-primary-foreground px-0.5">
            {unreadCount > 9 ? "9+" : unreadCount}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-80 bg-card border border-border rounded-lg shadow-xl z-50 overflow-hidden">
          <div className="px-3 py-2 border-b border-border">
            <span className="text-xs font-medium">{t("topbar.price_alerts")}</span>
          </div>
          {alerts.length === 0 ? (
            <p className="px-3 py-4 text-xs text-muted-foreground text-center">{t("topbar.no_alerts")}</p>
          ) : (
            <ul className="max-h-72 overflow-y-auto divide-y divide-border">
              {alerts.map((a) => (
                <li key={a.id} className="flex items-start justify-between gap-2 px-3 py-2.5">
                  <div className="text-xs space-y-0.5">
                    <span className="font-medium">
                      {a.symbol} ({a.market})
                    </span>
                    <p className="text-muted-foreground">
                      {a.condition === "above" ? t("topbar.price_above") : t("topbar.price_below")}{" "}
                      <span
                        className={
                          a.condition === "above" ? "text-green-400" : "text-red-400"
                        }
                      >
                        {a.target_price.toFixed(2)}
                      </span>{" "}
                      — {t("topbar.hit")}{" "}
                      <span className="text-foreground">{a.current_price.toFixed(2)}</span>
                    </p>
                  </div>
                  <button
                    onClick={() => dismiss(a.id)}
                    className="text-muted-foreground hover:text-foreground mt-0.5 text-base leading-none shrink-0"
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function ThemeToggle() {
  const { t } = useTranslation();
  const { theme, toggle } = useThemeStore();
  return (
    <button
      onClick={toggle}
      className="p-1.5 rounded hover:bg-accent/10 text-muted-foreground hover:text-foreground transition-colors"
      title={theme === "dark" ? t("topbar.switch_to_light") : t("topbar.switch_to_dark")}
    >
      <span className="text-base leading-none">{theme === "dark" ? "☀" : "🌙"}</span>
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
      className="px-1.5 py-0.5 rounded text-[11px] font-medium hover:bg-accent/10 text-muted-foreground hover:text-foreground transition-colors min-w-[28px]"
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
      className="lg:hidden p-1.5 -ml-1 rounded text-muted-foreground hover:text-foreground hover:bg-accent/10 transition-colors"
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <line x1="3" y1="6" x2="21" y2="6" />
        <line x1="3" y1="12" x2="21" y2="12" />
        <line x1="3" y1="18" x2="21" y2="18" />
      </svg>
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
        {/* Top bar */}
        <div className="h-10 border-b border-border flex items-center gap-2 sm:gap-3 px-3 sm:px-4 shrink-0">
          <MenuButton onOpen={() => setSidebarOpen(true)} />
          <GlobalSearch />
          <div className="flex items-center gap-1 sm:gap-2 ml-auto">
            <UpdateBadge />
            <WsStatus />
            <LanguageToggle />
            <ThemeToggle />
            <NotificationBell />
          </div>
        </div>
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
