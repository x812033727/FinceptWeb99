import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Loading } from "./_atoms";
import { fetchOptions, fmt, fmtK } from "./_shared";
import type { OptionRow } from "./_shared";
import { DataTable, type DataTableColumn } from "../ui/table";

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

  const rows = filtered.slice(0, 80);
  const columns: DataTableColumn<OptionRow>[] = [
    {
      key: "expiration_date",
      header: "Expiry",
      render: (o) => <span className="text-muted-foreground">{o.expiration_date ?? "—"}</span>,
    },
    {
      key: "strike",
      header: "Strike",
      numeric: true,
      render: (o) => <span className="font-medium text-foreground">{fmt(o.strike)}</span>,
    },
    {
      key: "bid",
      header: "Bid",
      numeric: true,
      render: (o) => <span className="text-foreground">{fmt(o.bid)}</span>,
    },
    {
      key: "ask",
      header: "Ask",
      numeric: true,
      render: (o) => <span className="text-foreground">{fmt(o.ask)}</span>,
    },
    {
      key: "last_price",
      header: "Last",
      numeric: true,
      render: (o) => <span className="text-foreground">{fmt(o.last_price)}</span>,
    },
    {
      key: "volume",
      header: "Volume",
      numeric: true,
      render: (o) => <span className="text-muted-foreground">{o.volume ? fmtK(o.volume) : "—"}</span>,
    },
    {
      key: "open_interest",
      header: "OI",
      numeric: true,
      render: (o) => <span className="text-muted-foreground">{o.open_interest ? fmtK(o.open_interest) : "—"}</span>,
    },
    {
      key: "implied_volatility",
      header: "IV",
      numeric: true,
      render: (o) => (
        <span className="text-muted-foreground">
          {o.implied_volatility ? `${(o.implied_volatility * 100).toFixed(1)}%` : "—"}
        </span>
      ),
    },
    {
      key: "delta",
      header: "Delta",
      numeric: true,
      render: (o) => <span className="text-muted-foreground">{fmt(o.delta, 3)}</span>,
    },
  ];

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
      {view === "table" && (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(_o, i) => i}
          mobileMode="scroll"
          aria-label="Options chain"
          empty={
            <div className="py-6 text-center text-muted-foreground text-xs">
              No {optionType} contracts in this chain.
            </div>
          }
        />
      )}
    </div>
  );
}
