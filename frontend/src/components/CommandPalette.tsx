import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "@/components/ui/command";
import { useSymbolSearch } from "@/hooks/useSymbolSearch";
import { prefetchPage } from "@/pageLoaders";
import { useAuthStore } from "@/store/authStore";
import { useThemeStore } from "@/store/themeStore";
import { logout } from "@/lib/auth";

interface PaletteContextValue {
  open: boolean;
  setOpen: (v: boolean) => void;
  toggle: () => void;
}

const PaletteContext = createContext<PaletteContextValue | null>(null);

export function useCommandPalette(): PaletteContextValue {
  const ctx = useContext(PaletteContext);
  if (!ctx) throw new Error("useCommandPalette must be used within <CommandPaletteProvider>");
  return ctx;
}

export function CommandPaletteProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const toggle = useCallback(() => setOpen((v) => !v), []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const value = useMemo(() => ({ open, setOpen, toggle }), [open, toggle]);

  return (
    <PaletteContext.Provider value={value}>
      {children}
      <CommandPaletteDialog />
    </PaletteContext.Provider>
  );
}

interface RouteEntry {
  to: string;
  labelKey: string;
  icon: string;
  adminOnly?: boolean;
}

const ROUTE_ENTRIES: RouteEntry[] = [
  { to: "/dashboard", labelKey: "nav.dashboard", icon: "⊞" },
  { to: "/market/US", labelKey: "nav.us_market", icon: "🇺🇸" },
  { to: "/market/TW", labelKey: "nav.tw_market", icon: "🇹🇼" },
  { to: "/market/CRYPTO", labelKey: "nav.crypto", icon: "₿" },
  { to: "/screener", labelKey: "nav.screener", icon: "🔍" },
  { to: "/macro", labelKey: "nav.macro", icon: "🌐" },
  { to: "/watchlist", labelKey: "nav.watchlist", icon: "⭐" },
  { to: "/alerts", labelKey: "nav.alerts", icon: "🔔" },
  { to: "/portfolio", labelKey: "nav.portfolio", icon: "📊" },
  { to: "/analytics", labelKey: "nav.analytics", icon: "📐" },
  { to: "/ai", labelKey: "nav.ai", icon: "🤖" },
  { to: "/discussion", labelKey: "nav.discussion", icon: "💬" },
  { to: "/finmind", labelKey: "nav.finmind", icon: "🧬" },
  { to: "/settings", labelKey: "nav.settings", icon: "⚙" },
  { to: "/admin", labelKey: "nav.admin", icon: "🛡", adminOnly: true },
];

function CommandPaletteDialog() {
  const { open, setOpen } = useCommandPalette();
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const role = useAuthStore((s) => s.user?.role);
  const toggleTheme = useThemeStore((s) => s.toggle);
  const [query, setQuery] = useState("");
  const { results: symbolResults } = useSymbolSearch(query);

  useEffect(() => {
    if (!open) {
      // Reset query when the dialog closes so reopening starts clean.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setQuery("");
    }
  }, [open]);

  function go(path: string) {
    setOpen(false);
    navigate(path);
  }

  const visibleRoutes = ROUTE_ENTRIES.filter((r) => !r.adminOnly || role === "admin");

  return (
    <CommandDialog open={open} onOpenChange={setOpen} title={t("palette.title")}>
      <CommandInput
        placeholder={t("palette.placeholder")}
        value={query}
        onValueChange={setQuery}
      />
      <CommandList>
        <CommandEmpty>{t("palette.empty")}</CommandEmpty>

        {symbolResults.length > 0 && (
          <>
            <CommandGroup heading={t("palette.section.symbols")}>
              {symbolResults.map((r) => (
                <CommandItem
                  key={`sym:${r.market}:${r.symbol}`}
                  value={`${r.symbol} ${r.market}`}
                  onSelect={() => go(`/stock/${r.market}/${r.symbol}`)}
                  onMouseEnter={() => prefetchPage(`/stock/${r.market}/${r.symbol}`)}
                >
                  <span className="font-medium">{r.symbol}</span>
                  <span className="ml-auto text-[10px] border border-border rounded px-1 text-muted-foreground">
                    {r.market}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
            <CommandSeparator />
          </>
        )}

        <CommandGroup heading={t("palette.section.pages")}>
          {visibleRoutes.map((r) => (
            <CommandItem
              key={r.to}
              value={`${t(r.labelKey)} ${r.to}`}
              onSelect={() => go(r.to)}
              onMouseEnter={() => prefetchPage(r.to)}
            >
              <span className="text-base leading-none w-4">{r.icon}</span>
              <span>{t(r.labelKey)}</span>
              <CommandShortcut>{r.to}</CommandShortcut>
            </CommandItem>
          ))}
        </CommandGroup>

        <CommandSeparator />

        <CommandGroup heading={t("palette.section.actions")}>
          <CommandItem
            value="theme toggle dark light"
            onSelect={() => {
              toggleTheme();
              setOpen(false);
            }}
          >
            <span className="text-base leading-none w-4">🎨</span>
            {t("palette.action.toggle_theme")}
          </CommandItem>
          <CommandItem
            value="language toggle locale"
            onSelect={() => {
              const next = i18n.language === "zh-TW" ? "en" : "zh-TW";
              void i18n.changeLanguage(next);
              setOpen(false);
            }}
          >
            <span className="text-base leading-none w-4">🌐</span>
            {t("palette.action.toggle_language")}
          </CommandItem>
          <CommandItem
            value="api keys settings"
            onSelect={() => go("/settings")}
          >
            <span className="text-base leading-none w-4">🔑</span>
            {t("palette.action.api_keys")}
          </CommandItem>
          <CommandItem
            value="sign out logout"
            onSelect={async () => {
              setOpen(false);
              await logout();
              navigate("/login");
            }}
          >
            <span className="text-base leading-none w-4">↩</span>
            {t("palette.action.sign_out")}
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
