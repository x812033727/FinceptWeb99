import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { CapturedSession } from "@/types/discussion";
import {
  CapturedSessionBadge,
  CapturedSessionInline,
} from "./CapturedSessionBadge";

describe("CapturedSessionBadge", () => {
  it("renders nothing when session is undefined", () => {
    // Old conclusions written before the freshness phase don't carry
    // captured_session; the badge must drop out cleanly so the
    // ConclusionCard layout doesn't shift around archive rows.
    const { container } = render(<CapturedSessionBadge session={undefined} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders the session date and the hint prose for backtest", () => {
    const session: CapturedSession = {
      session_date: "2025-06-01",
      phase: "backtest",
      is_intraday: false,
      hint_zh: "回測模式：截至 2025-06-01 收盤",
    };
    render(<CapturedSessionBadge session={session} />);
    // Date in monospace + the hint copy both visible — together they
    // are what stop the user from confusing the discussion's session
    // anchor with wall-clock NOW.
    expect(screen.getByText("2025-06-01")).toBeInTheDocument();
    expect(
      screen.getByText(/回測模式：截至 2025-06-01 收盤/),
    ).toBeInTheDocument();
  });

  it("renders the localized phase label", () => {
    const session: CapturedSession = {
      session_date: "2026-05-26",
      phase: "intraday",
      is_intraday: false,
      hint_zh: "TWSE 盤中",
    };
    render(<CapturedSessionBadge session={session} />);
    // English locale is locked in the test setup, so the i18n key
    // resolves to the English label — confirms the translation key
    // is wired correctly and not falling through to the raw key.
    expect(screen.getByText(/intraday/i)).toBeInTheDocument();
  });

  it("falls back to the raw phase key for an unknown phase", () => {
    // Backend could grow a new phase (e.g. extended-hours session)
    // before the frontend translation catches up — must render the
    // raw key instead of empty/broken instead of throwing.
    const session: CapturedSession = {
      session_date: "2026-05-27",
      phase: "extended_hours_future",
      is_intraday: true,
      hint_zh: "未來時段",
    };
    render(<CapturedSessionBadge session={session} />);
    expect(
      screen.getByText(/extended_hours_future/),
    ).toBeInTheDocument();
  });

  it("CapturedSessionInline renders a compact inline span", () => {
    const session: CapturedSession = {
      session_date: "2026-05-26",
      phase: "intraday",
      is_intraday: false,
      hint_zh: "skipped in inline variant",
    };
    render(<CapturedSessionInline session={session} />);
    expect(screen.getByText("2026-05-26")).toBeInTheDocument();
    // Inline variant deliberately drops the long hint — the row in
    // RoundContextsCard is space-constrained.
    expect(
      screen.queryByText(/skipped in inline variant/),
    ).not.toBeInTheDocument();
  });
});
