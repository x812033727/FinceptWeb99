import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QualitySignalsRow } from "./QualitySignalsRow";
import type { QualitySignals } from "@/types/discussion";

describe("QualitySignalsRow", () => {
  it("renders nothing when signals is undefined (pre-PR-1 conclusion)", () => {
    const { container } = render(<QualitySignalsRow signals={undefined} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows the skipped chip when _skipped is set", () => {
    const signals: QualitySignals = { _skipped: "parse_error" };
    render(<QualitySignalsRow signals={signals} />);
    expect(screen.getByText(/skipped/i)).toBeInTheDocument();
  });

  it("shows the green passed chip on a clean signal payload", () => {
    const signals: QualitySignals = {
      stance_distribution: { agree: 5, dissent: 1, supplement: 0, other: 0 },
      confidence_stats: { n: 3, mean: 0.55, over_confident: false },
      consensus_contradiction: false,
      hallucination_warnings: [],
    };
    render(<QualitySignalsRow signals={signals} />);
    expect(screen.getByText(/passed/i)).toBeInTheDocument();
  });

  it("shows the contradiction warning + over-confident warning when both fire", () => {
    const signals: QualitySignals = {
      stance_distribution: { agree: 1, dissent: 5, supplement: 0, other: 0 },
      confidence_stats: { n: 3, mean: 0.85, over_confident: true },
      consensus_contradiction: true,
      hallucination_warnings: [],
    };
    render(<QualitySignalsRow signals={signals} />);
    expect(screen.getByText(/contradiction/i)).toBeInTheDocument();
    expect(screen.getByText(/over-confident/i)).toBeInTheDocument();
    expect(screen.queryByText(/passed/i)).not.toBeInTheDocument();
  });

  it("expands the hallucination list when the count chip is clicked", async () => {
    const signals: QualitySignals = {
      hallucination_warnings: [
        { round: 1, persona_id: "soros", signal: "single_stock_futures_oi" },
        { round: 2, persona_id: "lynch", signal: "broker_concentration" },
      ],
    };
    render(<QualitySignalsRow signals={signals} />);
    // Triples are hidden initially.
    expect(screen.queryByText(/single_stock_futures_oi/)).not.toBeInTheDocument();
    // The count chip is a button.
    const button = screen.getByRole("button", { name: /hallucinated/i });
    await userEvent.click(button);
    expect(screen.getByText(/single_stock_futures_oi/)).toBeInTheDocument();
    expect(screen.getByText(/broker_concentration/)).toBeInTheDocument();
  });

  it("shows hallucination chip without the passed chip when warnings exist", () => {
    const signals: QualitySignals = {
      hallucination_warnings: [
        { round: 1, persona_id: "soros", signal: "single_stock_futures_oi" },
      ],
    };
    render(<QualitySignalsRow signals={signals} />);
    expect(screen.getByRole("button", { name: /hallucinated/i })).toBeInTheDocument();
    expect(screen.queryByText(/passed/i)).not.toBeInTheDocument();
  });
});
