import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { DataTable } from "./table";
import { StatCard } from "./StatCard";
import { Card } from "./card";
import { EmptyState } from "./EmptyState";
import { LoadingState } from "./LoadingState";
import { ChartTooltip } from "./ChartTooltip";

interface Row {
  id: string;
  n: number;
}
const rows: Row[] = [
  { id: "a", n: 1 },
  { id: "b", n: 2 },
];
const columns = [
  { key: "id", header: "ID" },
  { key: "n", header: "N", numeric: true },
];

describe("W2 primitive a11y additions", () => {
  it("DataTable: live wraps tbody in aria-live, loading sets aria-busy, caption is sr-only", () => {
    const { container } = render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        live
        loading
        caption="Test table"
        aria-label="t"
      />,
    );
    expect(container.querySelector("table")?.getAttribute("aria-busy")).toBe("true");
    expect(container.querySelector("tbody")?.getAttribute("aria-live")).toBe("polite");
    const cap = container.querySelector("caption");
    expect(cap?.textContent).toBe("Test table");
    expect(cap?.className).toContain("sr-only");
  });

  it("DataTable: no live/loading → no aria-live / aria-busy (default static)", () => {
    const { container } = render(
      <DataTable columns={columns} rows={rows} rowKey={(r) => r.id} aria-label="t" />,
    );
    expect(container.querySelector("table")?.getAttribute("aria-busy")).toBeNull();
    expect(container.querySelector("tbody")?.getAttribute("aria-live")).toBeNull();
  });

  it("StatCard: live announces the value region", () => {
    const { container } = render(<StatCard label="Idx" value="100" live />);
    expect(container.querySelector('[aria-live="polite"]')).not.toBeNull();
  });

  it("Card: surface prop selects the ladder rung", () => {
    const { container } = render(<Card surface="2">x</Card>);
    expect(container.firstElementChild?.className).toContain("bg-surface-2");
    expect(container.firstElementChild?.className).toContain("shadow-highlight");
  });

  it("EmptyState: role=status", () => {
    render(<EmptyState title="empty" />);
    expect(screen.getByRole("status")).toHaveTextContent("empty");
  });

  it("LoadingState: role=status + aria-live, falls back to a label", () => {
    render(<LoadingState />);
    const s = screen.getByRole("status");
    expect(s.getAttribute("aria-live")).toBe("polite");
    expect(s).toHaveTextContent("Loading");
  });
});

describe("ChartTooltip", () => {
  it("returns null when inactive or empty", () => {
    const { container } = render(<ChartTooltip active={false} payload={[{ name: "x", value: 1 }]} />);
    expect(container.firstChild).toBeNull();
    const { container: c2 } = render(<ChartTooltip active payload={[]} />);
    expect(c2.firstChild).toBeNull();
  });

  it("renders label and formatted values when active", () => {
    render(
      <ChartTooltip
        active
        label="2026-07"
        payload={[{ name: "Revenue", value: 1234, color: "#fff", dataKey: "revenue" }]}
        valueFormatter={(v) => `${v}M`}
      />,
    );
    expect(screen.getByText("2026-07")).toBeInTheDocument();
    expect(screen.getByText("Revenue")).toBeInTheDocument();
    expect(screen.getByText("1234M")).toBeInTheDocument();
  });
});
