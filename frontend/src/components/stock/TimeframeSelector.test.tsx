import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import TimeframeSelector from "./TimeframeSelector";
import { isIntradayTimeframe } from "./_shared";

describe("isIntradayTimeframe", () => {
  it("classifies 1m/5m/15m as intraday, 1d/1wk/1mo as daily-based", () => {
    expect(isIntradayTimeframe("1m")).toBe(true);
    expect(isIntradayTimeframe("5m")).toBe(true);
    expect(isIntradayTimeframe("15m")).toBe(true);
    expect(isIntradayTimeframe("1d")).toBe(false);
    expect(isIntradayTimeframe("1wk")).toBe(false);
    expect(isIntradayTimeframe("1mo")).toBe(false);
  });
});

describe("TimeframeSelector", () => {
  it("renders intraday and daily groups; clicks propagate to onChange", () => {
    const onChange = vi.fn();
    render(
      <TimeframeSelector value="1d" onChange={onChange} intradayAvailable={true} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "5m" }));
    expect(onChange).toHaveBeenCalledWith("5m");

    // Test locale is English → 日/週/月 render as D/W/M.
    fireEvent.click(screen.getByRole("button", { name: "W" }));
    expect(onChange).toHaveBeenCalledWith("1wk");
    fireEvent.click(screen.getByRole("button", { name: "M" }));
    expect(onChange).toHaveBeenCalledWith("1mo");
  });

  it("marks the active timeframe with aria-pressed", () => {
    render(
      <TimeframeSelector value="15m" onChange={() => {}} intradayAvailable={true} />,
    );
    expect(screen.getByRole("button", { name: "15m" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "D" })).toHaveAttribute("aria-pressed", "false");
  });

  it("disables intraday buttons with an explanatory tooltip when unavailable", () => {
    const onChange = vi.fn();
    render(
      <TimeframeSelector
        value="1d"
        onChange={onChange}
        intradayAvailable={false}
        coverageDays={30}
      />,
    );
    for (const label of ["1m", "5m", "15m"]) {
      const btn = screen.getByRole("button", { name: label });
      expect(btn).toBeDisabled();
      // Tooltip explains the retention-window limitation.
      expect(btn).toHaveAttribute("title", expect.stringContaining("30 days"));
      fireEvent.click(btn);
    }
    expect(onChange).not.toHaveBeenCalled();

    // Daily-based timeframes stay enabled regardless.
    const weekly = screen.getByRole("button", { name: "W" });
    expect(weekly).toBeEnabled();
    fireEvent.click(weekly);
    expect(onChange).toHaveBeenCalledWith("1wk");
  });

  it("still explains the coverage window on enabled intraday buttons", () => {
    render(
      <TimeframeSelector value="1m" onChange={() => {}} intradayAvailable={true} coverageDays={30} />,
    );
    expect(screen.getByRole("button", { name: "1m" }))
      .toHaveAttribute("title", expect.stringContaining("30 days"));
  });
});
