import { useEffect, useRef, useState } from "react";
import { Outlet } from "react-router-dom";
import Sidebar from "./Sidebar";
import { useAlertSocket } from "@/hooks/useWebSocket";
import { useNotificationStore } from "@/store/notificationStore";
import { useThemeStore } from "@/store/themeStore";

function NotificationBell() {
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
        title="Alerts"
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
            <span className="text-xs font-medium">Price Alerts</span>
          </div>
          {alerts.length === 0 ? (
            <p className="px-3 py-4 text-xs text-muted-foreground text-center">No alerts yet.</p>
          ) : (
            <ul className="max-h-72 overflow-y-auto divide-y divide-border">
              {alerts.map((a) => (
                <li key={a.id} className="flex items-start justify-between gap-2 px-3 py-2.5">
                  <div className="text-xs space-y-0.5">
                    <span className="font-medium">
                      {a.symbol} ({a.market})
                    </span>
                    <p className="text-muted-foreground">
                      Price {a.condition}{" "}
                      <span
                        className={
                          a.condition === "above" ? "text-green-400" : "text-red-400"
                        }
                      >
                        {a.target_price.toFixed(2)}
                      </span>{" "}
                      — hit{" "}
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
  const { theme, toggle } = useThemeStore();
  return (
    <button
      onClick={toggle}
      className="p-1.5 rounded hover:bg-accent/10 text-muted-foreground hover:text-foreground transition-colors"
      title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
    >
      <span className="text-base leading-none">{theme === "dark" ? "☀" : "🌙"}</span>
    </button>
  );
}

export default function AppLayout() {
  return (
    <div className="flex min-h-screen bg-background">
      <Sidebar />
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Top bar */}
        <div className="h-10 border-b border-border flex items-center justify-end gap-1 px-4 shrink-0">
          <ThemeToggle />
          <NotificationBell />
        </div>
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
