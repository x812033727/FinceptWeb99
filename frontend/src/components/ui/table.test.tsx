import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { DataTable, type DataTableColumn } from "./table";

interface Row {
  symbol: string;
  close: number;
  volume: number;
}

const rows: Row[] = [
  { symbol: "2330", close: 605, volume: 30000 },
  { symbol: "2317", close: 100.5, volume: 50000 },
];

const columns: DataTableColumn<Row>[] = [
  { key: "symbol", header: "Symbol", mobile: "primary" },
  { key: "close", header: "Close", numeric: true, render: (r) => r.close.toFixed(2), mobile: "primary" },
  { key: "volume", header: "Volume", numeric: true },
];

describe("DataTable", () => {
  it("renders headers and rows", () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.symbol} />);
    expect(screen.getByText("Symbol")).toBeTruthy();
    expect(screen.getByText("2330")).toBeTruthy();
    expect(screen.getByText("605.00")).toBeTruthy();
  });

  it("right-aligns numeric columns with tabular digits", () => {
    const { container } = render(
      <DataTable columns={columns} rows={rows} rowKey={(r) => r.symbol} />
    );
    const closeHeader = screen.getByText("Close").closest("th") as HTMLElement;
    expect(closeHeader.className).toContain("text-right");
    const numericCell = container.querySelector("td.text-right") as HTMLElement;
    expect(numericCell.className).toContain("tabular-nums");
  });

  it("falls back to row property and em-dash for missing values", () => {
    const sparse = [{ symbol: "0050", close: 0, volume: undefined as unknown as number }];
    render(<DataTable columns={columns} rows={sparse} rowKey={(r) => r.symbol} />);
    // volume has no render → property lookup → undefined → em-dash
    expect(screen.getByText("—")).toBeTruthy();
  });

  it("renders the empty slot when rows is empty", () => {
    render(
      <DataTable columns={columns} rows={[]} rowKey={(r: Row) => r.symbol} empty={<p>nothing here</p>} />
    );
    expect(screen.getByText("nothing here")).toBeTruthy();
    expect(document.querySelector("table")).toBeNull();
  });

  it("mobileMode='cards' renders both a table (sm+) and cards (<sm)", () => {
    const { container } = render(
      <DataTable columns={columns} rows={rows} rowKey={(r) => r.symbol} mobileMode="cards" />
    );
    const tableWrap = container.querySelector(".hidden.sm\\:block") as HTMLElement;
    expect(tableWrap.querySelector("table")).toBeTruthy();
    const cards = container.querySelector("ul.sm\\:hidden") as HTMLElement;
    expect(cards.querySelectorAll("li").length).toBe(2);
    // primary columns land in the card headline; secondary in the meta dl
    expect(cards.textContent).toContain("2330");
    expect(cards.querySelector("dl")?.textContent).toContain("Volume");
  });

  it("mobileMode='scroll' pins the first column sticky", () => {
    const { container } = render(
      <DataTable columns={columns} rows={rows} rowKey={(r) => r.symbol} mobileMode="scroll" />
    );
    const firstCell = container.querySelector("tbody td") as HTMLElement;
    expect(firstCell.className).toContain("sticky");
  });

  it("invokes onRowClick", () => {
    const onClick = vi.fn();
    render(
      <DataTable columns={columns} rows={rows} rowKey={(r) => r.symbol} onRowClick={onClick} />
    );
    fireEvent.click(screen.getByText("2330"));
    expect(onClick).toHaveBeenCalledWith(rows[0]);
  });
});
