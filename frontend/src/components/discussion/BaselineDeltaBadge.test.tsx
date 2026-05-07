import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { BaselineDeltaBadge } from "./BaselineDeltaBadge";
import type { Conclusion } from "@/types/discussion";

function makeConclusion(over: Partial<Conclusion> = {}): Conclusion {
  return {
    recommended_symbols: ["2330"],
    reasoning: "ok",
    risks: [],
    time_horizon: "short_term",
    consensus_score: 0.7,
    ...over,
  };
}

describe("BaselineDeltaBadge", () => {
  it("hides entirely when vs_baseline is missing", () => {
    const { container } = render(
      <BaselineDeltaBadge conclusion={makeConclusion()} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("hides when all three baselines are null (cold-start strategy)", () => {
    const { container } = render(
      <BaselineDeltaBadge
        conclusion={makeConclusion({
          vs_baseline: {
            brier_baseline: null,
            consensus_baseline: null,
            consensus_score: 0.7,
            consensus_pct_change: null,
            verify_pending: true,
          },
        })}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders consensus delta with positive sign + green tone when up >=5%", () => {
    const { container } = render(
      <BaselineDeltaBadge
        conclusion={makeConclusion({
          vs_baseline: {
            brier_baseline: null,
            consensus_baseline: 0.6,
            consensus_score: 0.75,
            consensus_pct_change: 25.0,
            verify_pending: true,
          },
        })}
      />,
    );
    expect(screen.getByText(/\+25%/)).toBeInTheDocument();
    expect(container.querySelector(".text-emerald-200")).toBeTruthy();
  });

  it("renders pending brier chip when verify_pending is true", () => {
    render(
      <BaselineDeltaBadge
        conclusion={makeConclusion({
          vs_baseline: {
            brier_baseline: 0.18,
            consensus_baseline: null,
            consensus_score: 0.7,
            consensus_pct_change: null,
            verify_pending: true,
          },
        })}
      />,
    );
    expect(screen.getByText(/Brier pending verifier/i)).toBeInTheDocument();
  });

  it("uses amber tone when consensus dropped below baseline by >=5%", () => {
    const { container } = render(
      <BaselineDeltaBadge
        conclusion={makeConclusion({
          vs_baseline: {
            brier_baseline: null,
            consensus_baseline: 0.7,
            consensus_score: 0.5,
            consensus_pct_change: -28.6,
            verify_pending: true,
          },
        })}
      />,
    );
    expect(container.querySelector(".text-amber-200")).toBeTruthy();
  });
});
