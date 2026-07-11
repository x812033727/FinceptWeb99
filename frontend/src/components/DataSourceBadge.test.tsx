import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { DataSourceBadge } from "./DataSourceBadge";

describe("DataSourceBadge", () => {
  it("renders nothing for steady-state sources (polygon / yfinance / twse / kraken)", () => {
    for (const source of ["polygon", "yfinance", "twse", "kraken"]) {
      const { container } = render(<DataSourceBadge source={source} />);
      expect(container.textContent).toBe("");
    }
  });

  it("renders nothing for undefined / null / empty source", () => {
    const cases: (string | undefined | null)[] = [undefined, null, ""];
    for (const source of cases) {
      const { container } = render(<DataSourceBadge source={source} />);
      expect(container.textContent).toBe("");
    }
  });

  it("renders the Stooq label + tooltip when source is stooq", () => {
    const { container } = render(<DataSourceBadge source="stooq" />);
    const span = container.querySelector("span");
    expect(span).toBeTruthy();
    expect(span!.textContent).toBe("Stooq");
    expect(span!.getAttribute("title")).toContain("Stooq fallback");
  });

  it("renders the EOD label + tooltip when source is finmind", () => {
    const { container } = render(<DataSourceBadge source="finmind" />);
    const span = container.querySelector("span");
    expect(span).toBeTruthy();
    expect(span!.textContent).toBe("EOD");
    expect(span!.getAttribute("title")).toContain("FinMind");
  });

  it("renders the unavailable em-dash label when all upstreams blocked", () => {
    const { container } = render(<DataSourceBadge source="unavailable" />);
    const span = container.querySelector("span");
    expect(span).toBeTruthy();
    expect(span!.textContent).toBe("—");
    expect(span!.getAttribute("title")).toContain("blocked");
    // Distinct red styling for the fully-blocked state vs amber for soft fallback.
    expect(span!.className).toContain("text-danger");
  });

  it("uses amber styling for soft-fallback sources (stooq / finmind)", () => {
    for (const source of ["stooq", "finmind"]) {
      const { container } = render(<DataSourceBadge source={source} />);
      const span = container.querySelector("span") as HTMLElement;
      expect(span.className).toContain("text-warning");
    }
  });
});
