import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import IndicatorToolbar from "./IndicatorToolbar";
import {
  loadIndicatorPrefs,
  saveIndicatorPrefs,
  INDICATOR_STORAGE_KEY,
  DEFAULT_INDICATOR_PREFS,
  type IndicatorPrefs,
} from "./indicatorPrefs";

beforeEach(() => {
  localStorage.clear();
});

describe("loadIndicatorPrefs / saveIndicatorPrefs", () => {
  it("round-trips preferences through localStorage", () => {
    const prefs: IndicatorPrefs = { overlays: ["ma", "boll"], sub: "macd" };
    saveIndicatorPrefs(prefs);
    expect(loadIndicatorPrefs()).toEqual(prefs);
  });

  it("returns defaults when nothing is stored", () => {
    expect(loadIndicatorPrefs()).toEqual(DEFAULT_INDICATOR_PREFS);
  });

  it("returns defaults on corrupt JSON", () => {
    localStorage.setItem(INDICATOR_STORAGE_KEY, "not-json{");
    expect(loadIndicatorPrefs()).toEqual(DEFAULT_INDICATOR_PREFS);
  });

  it("drops unknown keys instead of trusting stored values", () => {
    localStorage.setItem(
      INDICATOR_STORAGE_KEY,
      JSON.stringify({ overlays: ["ma", "bogus"], sub: "nope" })
    );
    expect(loadIndicatorPrefs()).toEqual({ overlays: ["ma"], sub: null });
  });
});

describe("IndicatorToolbar", () => {
  it("renders every chip and marks the active ones", () => {
    render(
      <IndicatorToolbar prefs={{ overlays: ["ma"], sub: "rsi" }} onChange={vi.fn()} />
    );
    for (const label of ["MA", "EMA", "BOLL", "RSI", "MACD", "KD", "Off"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "MA" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "EMA" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "RSI" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Off" })).toHaveAttribute("aria-pressed", "false");
  });

  it("toggles overlays independently (multi-select)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <IndicatorToolbar prefs={{ overlays: ["ma"], sub: null }} onChange={onChange} />
    );
    await user.click(screen.getByRole("button", { name: "EMA" }));
    expect(onChange).toHaveBeenLastCalledWith({ overlays: ["ma", "ema"], sub: null });
    await user.click(screen.getByRole("button", { name: "MA" }));
    expect(onChange).toHaveBeenLastCalledWith({ overlays: [], sub: null });
  });

  it("treats the sub-pane group as single-select with Off and re-tap-to-clear", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = render(
      <IndicatorToolbar prefs={{ overlays: [], sub: null }} onChange={onChange} />
    );
    await user.click(screen.getByRole("button", { name: "MACD" }));
    expect(onChange).toHaveBeenLastCalledWith({ overlays: [], sub: "macd" });

    rerender(<IndicatorToolbar prefs={{ overlays: [], sub: "macd" }} onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "KD" }));
    expect(onChange).toHaveBeenLastCalledWith({ overlays: [], sub: "kd" });
    await user.click(screen.getByRole("button", { name: "MACD" }));
    expect(onChange).toHaveBeenLastCalledWith({ overlays: [], sub: null });
    await user.click(screen.getByRole("button", { name: "Off" }));
    expect(onChange).toHaveBeenLastCalledWith({ overlays: [], sub: null });
  });
});
