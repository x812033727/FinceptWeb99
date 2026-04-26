import { useEffect, useRef, useState } from "react";
import { Outlet, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import Sidebar from "./Sidebar";
import UpdateBadge from "./UpdateBadge";
import { useAlertSocket, useWsConnected } from "@/hooks/useWebSocket";
import { useNotificationStore } from "@/store/notificationStore";
import { useThemeStore } from "@/store/themeStore";
import api from "@/lib/api";

type SearchResult = { symbol: string; market: "US" | "TW" };

function GlobalSearch() {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [open, setOpen] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (query.trim().length === 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setResults([]);
      setOpen(false);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      try {
        const [usRes, twRes] = await Promise.allSettled([
          api.get<SearchResult[]>(`/us/search?q=${encodeURIComponent(query)}&limit=7`),
          api.get<SearchResult[]>(`/tw/search?q=${encodeURIComponent(query)}&limit=3`),
        ]);
        const combined: SearchResult[] = [
          ...(usRes.status === "fulfilled" ? usRes.value.data : []),
          ...(twRes.status === "fulfilled" ? twRes.value.data : []),
        ];
        setResults(combined.slice(0, 10));
        setActiveIdx(0);
        setOpen(combined.length > 0);
      } catch {
        setResults([]);
        setOpen(false);
      }
    }, 300);
  }, [query]);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (
        inputRef.current &&
        !inputRef.current.contains(e.target as Node) &&
        dropdownRef.current &&
        !dropdownRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  function selectResult(r: SearchResult) {
    navigate(`/stock/${r.market}/${r.symbol}`);
    setQuery("");
    setOpen(false);
    inputRef.current?.blur();
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open || results.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => (i + 1) % results.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => (i - 1 + results.length) % results.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      selectResult(results[activeIdx]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="relative flex-1 max-w-xs">
      <input
        ref={inputRef}
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={handleKeyDown}
        onFocus={() => results.length > 0 && setOpen(true)}
        placeholder={t("topbar.search_placeholder")}
        className="w-full h-7 rounded bg-muted/40 border border-border px-3 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
      />
      {open && results.length > 0 && (
        <div
          ref={dropdownRef}
          className="absolute top-full left-0 mt-1 w-full bg-card border border-border rounded-lg shadow-xl z-50 overflow-hidden"
        >
          {results.map((r, i) => (
            <button
              key={`${r.market}:${r.symbol}`}
              onMouseDown={(e) => { e.preventDefault(); selectResult(r); }}
              onMouseEnter={() => setActiveIdx(i)}
              className={`w-full flex items-center gap-2 px-3 py-2 text-xs text-left transition-colors ${
                i === activeIdx ? "bg-accent/20 text-foreground" : "hover:bg-accent/10 text-foreground"
              }`}
            >
              <span className="font-medium">{r.symbol}</span>
              <span className="ml-auto text-muted-foreground text-[10px] border border-border rounded px-1">
                {r.market}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

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

export default function AppLayout() {
  return (
    <div className="flex min-h-screen bg-background">
      <Sidebar />
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Top bar */}
        <div className="h-10 border-b border-border flex items-center gap-3 px-4 shrink-0">
          <GlobalSearch />
          <div className="flex items-center gap-2 ml-auto">
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
