import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Loading } from "./_atoms";
import { fetchOptions, fmt, fmtK } from "./_shared";
import type { OptionRow } from "./_shared";

function IVSurface({ options, optionType }: { options: OptionRow[]; optionType: "call" | "put" }) {
  const filtered = options.filter(
    (o) => o.contract_type?.toLowerCase() === optionType && o.implied_volatility != null
  );
  if (!filtered.length) return null;

  const expiries = [...new Set(filtered.map((o) => o.expiration_date).filter(Boolean))].sort().slice(0, 8);
  const strikes = [...new Set(filtered.map((o) => o.strike))].sort((a, b) => a - b);
  // Take ~12 strikes centred around median
  const mid = Math.floor(strikes.length / 2);
  const slicedStrikes = strikes.slice(Math.max(0, mid - 6), mid + 6);

  // iv lookup
  const ivMap: Record<string, number> = {};
  filtered.forEach((o) => {
    if (o.expiration_date && o.strike) {
      ivMap[`${o.expiration_date}:${o.strike}`] = (o.implied_volatility ?? 0) * 100;
    }
  });

  const allIVs = Object.values(ivMap).filter(Boolean);
  const minIV = Math.min(...allIVs);
  const maxIV = Math.max(...allIVs);

  function ivColor(iv: number | undefined): string {
    if (!iv || maxIV === minIV) return "hsl(var(--card))";
    const t = (iv - minIV) / (maxIV - minIV);
    // low IV = blue, high IV = red
    const r = Math.round(t * 239);
    const g = Math.round((1 - Math.abs(t - 0.5) * 2) * 120);
    const b = Math.round((1 - t) * 239);
    return `rgb(${r},${g},${b})`;
  }

  if (!expiries.length || !slicedStrikes.length) return null;

  return (
    <div className="mt-4">
      <h4 className="text-xs font-semibold text-foreground mb-2">
        Implied Volatility Surface ({optionType.toUpperCase()})
      </h4>
      <div className="overflow-x-auto">
        <table className="text-xs border-collapse">
          <thead>
            <tr>
              <th className="text-left pr-3 pb-1 text-muted-foreground font-medium">Strike \ Expiry</th>
              {expiries.map((e) => (
                <th key={e} className="px-1 pb-1 text-muted-foreground font-medium text-center min-w-[60px]">
                  {e?.slice(5)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {slicedStrikes.map((strike) => (
              <tr key={strike}>
                <td className="pr-3 py-0.5 text-muted-foreground">{strike}</td>
                {expiries.map((expiry) => {
                  const iv = ivMap[`${expiry}:${strike}`];
                  return (
                    <td
                      key={expiry}
                      className="text-center py-0.5 px-1 rounded text-[10px] font-medium"
                      style={{
                        backgroundColor: ivColor(iv),
                        color: iv ? "#f9fafb" : "transparent",
                        minWidth: 52,
                      }}
                    >
                      {iv ? `${iv.toFixed(1)}%` : "·"}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        <div className="flex items-center gap-2 mt-2 text-[10px] text-muted-foreground">
          <span>Low IV</span>
          <div className="flex h-2 w-24 rounded overflow-hidden">
            {Array.from({ length: 24 }).map((_, i) => (
              <div key={i} style={{ flex: 1, backgroundColor: ivColor(minIV + (i / 23) * (maxIV - minIV)) }} />
            ))}
          </div>
          <span>High IV</span>
        </div>
      </div>
    </div>
  );
}

export function OptionsPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const [optionType, setOptionType] = useState<"call" | "put">("call");
  const [view, setView] = useState<"table" | "surface">("table");

  const { data: options = [], isLoading } = useQuery({
    queryKey: ["options", symbol],
    queryFn: () => fetchOptions(symbol),
    staleTime: 300_000,
  });

  if (isLoading) return <Loading />;
  if (!options.length) return <div className="p-6 text-muted-foreground text-sm">No options data available.</div>;

  // Get available expiry dates
  const expiries = [...new Set(options.map((o) => o.expiration_date).filter(Boolean))].sort();
  const filtered = options.filter((o) => o.contract_type?.toLowerCase() === optionType);
  // Per-row tagging from the service layer. Polygon-served chains have
  // bid/ask spreads; the yfinance fallback (free-tier default) doesn't.
  const isYFinanceChain = options[0]?.data_source === "yfinance";

  return (
    <div className="p-4 space-y-4">
      {isYFinanceChain && (
        <div className="text-xs text-muted-foreground bg-muted/30 border border-border rounded px-3 py-2">
          {t("stock.options_hint_yfinance")}
        </div>
      )}
      {/* controls */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex rounded border border-border overflow-hidden text-sm">
          {(["call", "put"] as const).map((tt) => (
            <button
              key={tt}
              onClick={() => setOptionType(tt)}
              className={`px-4 py-1.5 transition-colors ${
                optionType === tt
                  ? tt === "call" ? "bg-up/15 text-up" : "bg-down/15 text-down"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {tt.toUpperCase()}
            </button>
          ))}
        </div>
        <div className="flex rounded border border-border overflow-hidden text-sm">
          {(["table", "surface"] as const).map((v) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={`px-3 py-1.5 transition-colors ${
                view === v ? "bg-primary/20 text-primary" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {v === "table" ? "Table" : "IV Surface"}
            </button>
          ))}
        </div>
        <span className="text-xs text-muted-foreground">{filtered.length} contracts</span>
      </div>

      {/* expiry legend */}
      {view === "table" && expiries.length > 0 && (
        <div className="text-xs text-muted-foreground">
          Available expirations: {expiries.slice(0, 6).join(" · ")}{expiries.length > 6 ? " …" : ""}
        </div>
      )}

      {/* IV surface */}
      {view === "surface" && <IVSurface options={options} optionType={optionType} />}

      {/* table */}
      {view === "table" && <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left py-2 pr-3 font-medium">Expiry</th>
              <th className="text-right py-2 px-2 font-medium">Strike</th>
              <th className="text-right py-2 px-2 font-medium">Bid</th>
              <th className="text-right py-2 px-2 font-medium">Ask</th>
              <th className="text-right py-2 px-2 font-medium">Last</th>
              <th className="text-right py-2 px-2 font-medium">Volume</th>
              <th className="text-right py-2 px-2 font-medium">OI</th>
              <th className="text-right py-2 px-2 font-medium">IV</th>
              <th className="text-right py-2 px-2 font-medium">Delta</th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 && (
              <tr>
                <td colSpan={9} className="py-6 text-center text-muted-foreground text-xs">
                  No {optionType} contracts in this chain.
                </td>
              </tr>
            )}
            {filtered.slice(0, 80).map((o, i) => (
              <tr key={i} className="border-b border-border/30 hover:bg-accent/5">
                <td className="py-1.5 pr-3 text-muted-foreground">{o.expiration_date ?? "—"}</td>
                <td className="text-right py-1.5 px-2 font-medium text-foreground">{fmt(o.strike)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmt(o.bid)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmt(o.ask)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmt(o.last_price)}</td>
                <td className="text-right py-1.5 px-2 text-muted-foreground">{o.volume ? fmtK(o.volume) : "—"}</td>
                <td className="text-right py-1.5 px-2 text-muted-foreground">{o.open_interest ? fmtK(o.open_interest) : "—"}</td>
                <td className="text-right py-1.5 px-2 text-muted-foreground">
                  {o.implied_volatility ? `${(o.implied_volatility * 100).toFixed(1)}%` : "—"}
                </td>
                <td className="text-right py-1.5 px-2 text-muted-foreground">{fmt(o.delta, 3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>}
    </div>
  );
}
