import { render, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { dedupeBySymbol } from "./dailyCandidates";
import { Scoreboard, SessionBadge } from "./DailyPage";

const baseEntry = {
  strategy: "price_signal",
  samples: 40,
  pool_samples: 40,
  wins: 0,
  losses: 0,
  big_wins: 0,
  big_losses: 0,
  unverifiable: 0,
};

// Render Scoreboard and return the verdict-lens and D5-lens win-rate cells
// for the single row, so tests can assert each lens's dimming independently.
function renderRow(entry: Record<string, unknown>) {
  const { container } = render(<Scoreboard entries={[{ ...baseEntry, ...entry }]} />);
  const cells = container.querySelectorAll("tbody td");
  return { nameCell: cells[0], verdictCell: cells[1], d5Cell: cells[2] };
}

const DIM = "text-slate-400";

describe("dedupeBySymbol", () => {
  it("keeps the highest-scored entry per symbol and sorts descending", () => {
    const items = [
      { symbol: "2330", strategy_score: 10 },
      { symbol: "1101", strategy_score: 20, signal_type: "oversold" },
      { symbol: "2330", strategy_score: 15, signal_type: "breakout" },
      { symbol: undefined, strategy_score: 99 },
    ];
    expect(dedupeBySymbol(items)).toEqual([
      { symbol: "1101", strategy_score: 20, signal_type: "oversold" },
      { symbol: "2330", strategy_score: 15, signal_type: "breakout" },
    ]);
  });

  it("tolerates entries without scores", () => {
    expect(dedupeBySymbol([{ symbol: "2330" }, { symbol: "2330", strategy_score: 1 }])).toEqual([
      { symbol: "2330", strategy_score: 1 },
    ]);
  });
});

describe("SessionBadge", () => {
  it("renders a muted 「資料截至」 pill for a live session, not the backtest label", () => {
    const { getByText, queryByText } = render(
      <SessionBadge session={{ session_date: "2026-07-24", phase: "today_close_published" }} />,
    );
    expect(getByText("資料截至 2026-07-24")).toBeTruthy();
    expect(getByText("資料截至 2026-07-24").className).not.toContain("amber");
    expect(queryByText(/回測重播/)).toBeNull();
  });

  it("switches to the warning-styled 「回測重播」 pill when phase is backtest", () => {
    const { getByText } = render(
      <SessionBadge session={{ session_date: "2025-06-01", phase: "backtest" }} />,
    );
    const pill = getByText("回測重播 · 2025-06-01");
    expect(pill.className).toContain("amber");
  });

  it("puts hint_zh in the pill's title attribute for the hover tooltip", () => {
    const { getByText } = render(
      <SessionBadge session={{ session_date: "2025-06-01", phase: "backtest", hint_zh: "回測模式：截至 2025-06-01 收盤" }} />,
    );
    expect(getByText("回測重播 · 2025-06-01").title).toBe("回測模式：截至 2025-06-01 收盤");
  });

  it("renders nothing and does not crash when there is no captured_session", () => {
    const { container: withNull } = render(<SessionBadge session={null} />);
    expect(withNull.textContent).toBe("");
    const { container: withUndefined } = render(<SessionBadge session={undefined} />);
    expect(withUndefined.textContent).toBe("");
    const { container: withEmptyObject } = render(<SessionBadge session={{}} />);
    expect(withEmptyObject.textContent).toBe("");
  });
});

describe("Scoreboard sample-sufficiency markers", () => {
  it("marks the D5 lens thin off its own settled count, not the verdict lens's", () => {
    // Verdict lens well-sampled (20 decided), D5 lens thin (2 settled).
    const { nameCell, verdictCell, d5Cell } = renderRow({
      decided: 20, win_rate: 0.5, wins: 10, losses: 10,
      d5_decided: 2, d5_wins: 2, d5_losses: 0, d5_win_rate: 1, d5_unsettled: 18,
    });
    // Verdict lens is NOT thin: no row marker, its % is not dimmed.
    expect(within(nameCell as HTMLElement).queryByText("樣本不足")).toBeNull();
    expect(within(verdictCell as HTMLElement).getByText("50%").className).not.toContain(DIM);
    // D5 lens IS thin: its % is dimmed and its own 樣本不足 marker shows.
    expect(within(d5Cell as HTMLElement).getByText("100%").className).toContain(DIM);
    expect(within(d5Cell as HTMLElement).getByText("樣本不足")).toBeTruthy();
  });

  it("keeps the D5 lens solid when it is well-sampled but the verdict lens is thin", () => {
    // Verdict lens thin (3 decided), D5 lens well-sampled (15 settled).
    const { nameCell, verdictCell, d5Cell } = renderRow({
      decided: 3, win_rate: 1, wins: 3, losses: 0,
      d5_decided: 15, d5_wins: 9, d5_losses: 6, d5_win_rate: 0.6, d5_unsettled: 0,
    });
    // Verdict lens IS thin: row marker present, its % dimmed.
    expect(within(nameCell as HTMLElement).getByText("樣本不足")).toBeTruthy();
    expect(within(verdictCell as HTMLElement).getByText("100%").className).toContain(DIM);
    // D5 lens is NOT thin: its % not dimmed, no D5 marker.
    expect(within(d5Cell as HTMLElement).getByText("60%").className).not.toContain(DIM);
    expect(within(d5Cell as HTMLElement).queryByText("樣本不足")).toBeNull();
  });
});
