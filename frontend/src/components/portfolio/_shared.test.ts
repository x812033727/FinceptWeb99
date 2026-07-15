import { describe, expect, it } from "vitest";
import { parseTransactionCSV } from "./_shared";

describe("parseTransactionCSV", () => {
  it("accepts exported aliases, quoted notes, and optional FX", () => {
    const rows = parseTransactionCSV([
      "date,symbol,market,type,quantity,price,fx_rate,notes",
      '2024-01-02,AAPL,US,buy,10,185.5,1,"first, lot"',
      "2024-01-03,2330,TW,sell,2,600,,",
    ].join("\r\n"));

    expect(rows).toEqual([
      {
        tx_date: "2024-01-02", symbol: "AAPL", market: "US", tx_type: "buy",
        quantity: 10, price: 185.5, fx_rate: 1, notes: "first, lot",
      },
      {
        tx_date: "2024-01-03", symbol: "2330", market: "TW", tx_type: "sell",
        quantity: 2, price: 600,
      },
    ]);
  });

  it("rejects a file without required columns", () => {
    expect(() => parseTransactionCSV("symbol,price\nAAPL,10"))
      .toThrow("Missing columns");
  });
});
