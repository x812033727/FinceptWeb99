import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { RoundDivider } from "./RoundDivider";

describe("RoundDivider", () => {
  it("renders the round number", () => {
    render(<RoundDivider round={3} />);
    // i18n is set to en in test setup → "Round 3".
    expect(screen.getByText("Round 3")).toBeInTheDocument();
  });

  it("renders the round summary label when turnCount is provided", () => {
    render(<RoundDivider round={2} turnCount={5} />);
    // Locale renders "Round 2 · 5 turns".
    expect(screen.getByText(/Round 2/)).toBeInTheDocument();
    expect(screen.getByText(/5/)).toBeInTheDocument();
  });

  it("exposes role=separator with the label as aria-label for screen readers", () => {
    render(<RoundDivider round={1} turnCount={4} />);
    const sep = screen.getByRole("separator");
    expect(sep.getAttribute("aria-label") ?? "").toMatch(/Round 1/);
  });
});
