import type { ReactElement } from "react";
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { Num, DeltaText } from "./Num";

function spanOf(ui: ReactElement): HTMLElement {
  const { container } = render(ui);
  return container.querySelector("span") as HTMLElement;
}

describe("Num", () => {
  it("renders locale-formatted number with font-mono tabular-nums", () => {
    const span = spanOf(<Num value={1234.5} />);
    expect(span.textContent).toBe((1234.5).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }));
    expect(span.className).toContain("font-mono");
    expect(span.className).toContain("tabular-nums");
  });

  it("respects decimals", () => {
    const span = spanOf(<Num value={3.14159} decimals={4} />);
    expect(span.textContent).toBe("3.1416");
  });

  it("format='percent' renders unsigned percent (value already in % units)", () => {
    const span = spanOf(<Num value={9.93} format="percent" />);
    expect(span.textContent).toBe("9.93%");
  });

  it("format='compact' renders K / M / B suffixes", () => {
    expect(spanOf(<Num value={1_500_000} format="compact" />).textContent).toBe("1.5M");
    expect(spanOf(<Num value={2_000_000_000} format="compact" />).textContent).toBe("2.00B");
  });

  it("renders em-dash for null / undefined", () => {
    expect(spanOf(<Num value={null} />).textContent).toBe("—");
    expect(spanOf(<Num value={undefined} />).textContent).toBe("—");
  });

  it("appends custom className", () => {
    const span = spanOf(<Num value={1} className="text-lg" />);
    expect(span.className).toContain("text-lg");
  });
});

describe("DeltaText", () => {
  it("positive value gets '+' sign and text-up", () => {
    const span = spanOf(<DeltaText value={1.23} />);
    expect(span.textContent).toBe("+1.23");
    expect(span.className).toContain("text-up");
    expect(span.className).not.toContain("text-down");
  });

  it("negative value gets '-' sign and text-down", () => {
    const span = spanOf(<DeltaText value={-2.5} />);
    expect(span.textContent).toBe("-2.50");
    expect(span.className).toContain("text-down");
  });

  it("zero renders text-flat", () => {
    const span = spanOf(<DeltaText value={0} />);
    expect(span.textContent).toBe("+0.00");
    expect(span.className).toContain("text-flat");
  });

  it("percent suffix uses the shared formatPct convention", () => {
    expect(spanOf(<DeltaText value={9.93} percent />).textContent).toBe("+9.93%");
    expect(spanOf(<DeltaText value={-2.5} percent />).textContent).toBe("-2.50%");
  });

  it("null / undefined render a flat em-dash", () => {
    for (const v of [null, undefined]) {
      const span = spanOf(<DeltaText value={v} />);
      expect(span.textContent).toBe("—");
      expect(span.className).toContain("text-flat");
    }
  });

  it("uses font-mono tabular-nums and no hardcoded red/green classes", () => {
    const span = spanOf(<DeltaText value={5} />);
    expect(span.className).toContain("font-mono");
    expect(span.className).toContain("tabular-nums");
    expect(span.className).not.toMatch(/red|green/);
  });
});
