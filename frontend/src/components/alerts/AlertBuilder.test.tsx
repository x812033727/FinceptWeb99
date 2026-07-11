/**
 * AlertBuilder (PR-D1): condition-type select drives the dynamic
 * params sub-form; submit builds the rule-engine payload shape the
 * backend expects (condition_type + params + repeat + cooldown).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import AlertBuilder, { AlertRulePayload } from "./AlertBuilder";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string) => k,
    i18n: { language: "en", changeLanguage: vi.fn() },
  }),
}));

function setup(onSubmit = vi.fn()) {
  render(<AlertBuilder onSubmit={onSubmit} isPending={false} />);
  return onSubmit;
}

function fill(labelKey: string, value: string) {
  fireEvent.change(screen.getByLabelText(labelKey), { target: { value } });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: "alerts.create" }));
}

describe("AlertBuilder dynamic params sub-form", () => {
  it("shows the target-price input for the default price rule", () => {
    setup();
    expect(screen.getByLabelText("alerts.target_price")).toBeTruthy();
    expect(screen.queryByLabelText("alerts.param_pct")).toBeNull();
  });

  it("switches to the pct input for pct-change rules", () => {
    setup();
    fill("alerts.condition_type", "pct_change_above");
    expect(screen.getByLabelText("alerts.param_pct")).toBeTruthy();
    expect(screen.queryByLabelText("alerts.target_price")).toBeNull();
  });

  it("shows lookback days for breakout rules", () => {
    setup();
    fill("alerts.condition_type", "breakout_high");
    expect(screen.getByLabelText("alerts.param_lookback_days")).toBeTruthy();
  });

  it("shows multiple + lookback for volume surge", () => {
    setup();
    fill("alerts.condition_type", "volume_surge");
    expect(screen.getByLabelText("alerts.param_multiple")).toBeTruthy();
    expect(screen.getByLabelText("alerts.param_lookback_days")).toBeTruthy();
  });

  it("only offers the TW-only streak rule when market is TW", () => {
    setup();
    const options = () =>
      Array.from(
        screen.getByLabelText("alerts.condition_type").querySelectorAll("option")
      ).map((o) => (o as HTMLOptionElement).value);
    expect(options()).not.toContain("foreign_net_buy_streak");
    fill("alerts.market", "TW");
    expect(options()).toContain("foreign_net_buy_streak");
  });

  it("resets a TW-only rule when switching away from TW", () => {
    setup();
    fill("alerts.market", "TW");
    fill("alerts.condition_type", "foreign_net_buy_streak");
    expect(screen.getByLabelText("alerts.param_days")).toBeTruthy();
    fill("alerts.market", "US");
    const select = screen.getByLabelText("alerts.condition_type") as HTMLSelectElement;
    expect(select.value).toBe("price_above");
  });
});

describe("AlertBuilder submit payload", () => {
  it("builds a legacy-compatible price rule payload", () => {
    const onSubmit = setup();
    fill("alerts.symbol", "aapl");
    fill("alerts.target_price", "200");
    submit();
    expect(onSubmit).toHaveBeenCalledWith({
      symbol: "AAPL",
      market: "US",
      condition_type: "price_above",
      target_price: 200,
      params: null,
      repeat: false,
      cooldown_seconds: 0,
    } satisfies AlertRulePayload);
  });

  it("builds a pct-change payload with repeat + cooldown from the freq select", () => {
    const onSubmit = setup();
    fill("alerts.symbol", "NVDA");
    fill("alerts.condition_type", "pct_change_above");
    fill("alerts.param_pct", "5");
    fill("alerts.freq", "1h");
    submit();
    expect(onSubmit).toHaveBeenCalledWith({
      symbol: "NVDA",
      market: "US",
      condition_type: "pct_change_above",
      target_price: null,
      params: { pct: 5 },
      repeat: true,
      cooldown_seconds: 3600,
    });
  });

  it("builds a volume-surge payload with multiple + lookback", () => {
    const onSubmit = setup();
    fill("alerts.symbol", "2330");
    fill("alerts.market", "TW");
    fill("alerts.condition_type", "volume_surge");
    fill("alerts.param_multiple", "3");
    fill("alerts.param_lookback_days", "10");
    fill("alerts.freq", "1d");
    submit();
    expect(onSubmit).toHaveBeenCalledWith({
      symbol: "2330",
      market: "TW",
      condition_type: "volume_surge",
      target_price: null,
      params: { multiple: 3, lookback_days: 10 },
      repeat: true,
      cooldown_seconds: 86400,
    });
  });

  it("builds a streak payload with days", () => {
    const onSubmit = setup();
    fill("alerts.symbol", "2330");
    fill("alerts.market", "TW");
    fill("alerts.condition_type", "foreign_net_buy_streak");
    fill("alerts.param_days", "5");
    submit();
    expect(onSubmit).toHaveBeenCalledWith({
      symbol: "2330",
      market: "TW",
      condition_type: "foreign_net_buy_streak",
      target_price: null,
      params: { days: 5 },
      repeat: false,
      cooldown_seconds: 0,
    });
  });

  it("blocks submit and shows an error when the symbol is missing", () => {
    const onSubmit = setup();
    submit();
    expect(onSubmit).not.toHaveBeenCalled();
    // the error <p> carries the danger token (the field label matches
    // the same text, so scope by class)
    expect(document.querySelector("p.text-danger")?.textContent).toBe(
      "alerts.symbol"
    );
  });

  it("blocks submit on invalid params", () => {
    const onSubmit = setup();
    fill("alerts.symbol", "AAPL");
    fill("alerts.condition_type", "pct_change_above");
    // pct left empty
    submit();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("alerts.invalid_params")).toBeTruthy();
  });
});
