import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  Activity,
  Bell,
  Bitcoin,
  Bot,
  Briefcase,
  Calculator,
  Database,
  Globe,
  KeyRound,
  LayoutGrid,
  LineChart,
  LogOut,
  MessagesSquare,
  Palette,
  Search,
  Settings as SettingsIcon,
  Shield,
  Star,
  type LucideIcon,
} from "lucide-react";
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
import { useCommandPalette, PaletteContext } from "@/hooks/useCommandPalette";
import { prefetchPage } from "@/pageLoaders";
import { useAuthStore } from "@/store/authStore";
import { useThemeStore } from "@/store/themeStore";
import { logout } from "@/lib/auth";

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
  Icon: LucideIcon;
  adminOnly?: boolean;
}

const ROUTE_ENTRIES: RouteEntry[] = [
  { to: "/dashboard", labelKey: "nav.dashboard", Icon: LayoutGrid },
  { to: "/market/US", labelKey: "nav.us_market", Icon: LineChart },
  { to: "/market/TW", labelKey: "nav.tw_market", Icon: Activity },
  { to: "/market/CRYPTO", labelKey: "nav.crypto", Icon: Bitcoin },
  { to: "/screener", labelKey: "nav.screener", Icon: Search },
  { to: "/macro", labelKey: "nav.macro", Icon: Globe },
  { to: "/watchlist", labelKey: "nav.watchlist", Icon: Star },
  { to: "/alerts", labelKey: "nav.alerts", Icon: Bell },
  { to: "/portfolio", labelKey: "nav.portfolio", Icon: Briefcase },
  { to: "/analytics", labelKey: "nav.analytics", Icon: Calculator },
  { to: "/ai", labelKey: "nav.ai", Icon: Bot },
  { to: "/discussion", labelKey: "nav.discussion", Icon: MessagesSquare },
  { to: "/finmind", labelKey: "nav.finmind", Icon: Database },
  { to: "/settings", labelKey: "nav.settings", Icon: SettingsIcon },
  { to: "/admin", labelKey: "nav.admin", Icon: Shield, adminOnly: true },
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
          {visibleRoutes.map((r) => {
            const Icon = r.Icon;
            return (
              <CommandItem
                key={r.to}
                value={`${t(r.labelKey)} ${r.to}`}
                onSelect={() => go(r.to)}
                onMouseEnter={() => prefetchPage(r.to)}
              >
                <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                <span>{t(r.labelKey)}</span>
                <CommandShortcut>{r.to}</CommandShortcut>
              </CommandItem>
            );
          })}
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
            <Palette className="h-4 w-4 shrink-0" aria-hidden="true" />
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
            <Globe className="h-4 w-4 shrink-0" aria-hidden="true" />
            {t("palette.action.toggle_language")}
          </CommandItem>
          <CommandItem
            value="api keys settings"
            onSelect={() => go("/settings")}
          >
            <KeyRound className="h-4 w-4 shrink-0" aria-hidden="true" />
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
            <LogOut className="h-4 w-4 shrink-0" aria-hidden="true" />
            {t("palette.action.sign_out")}
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
