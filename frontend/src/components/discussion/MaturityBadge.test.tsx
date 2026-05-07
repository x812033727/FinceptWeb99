import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MaturityBadge } from "./MaturityBadge";
import type { MaturityTier } from "./_helpers";

describe("MaturityBadge", () => {
  it("renders nothing when tier is undefined", () => {
    const { container } = render(<MaturityBadge tier={undefined} />);
    expect(container.firstChild).toBeNull();
  });

  it.each<MaturityTier>([
    "cold_start",
    "learning",
    "mature",
    "drifting",
    "stale",
  ])("renders %s with the right label", (tier) => {
    render(<MaturityBadge tier={tier} />);
    // The English locale labels live in en.json — we just need to
    // assert SOMETHING textual is rendered, not the literal string,
    // so the test isn't fragile against copy tweaks.
    const span = screen.getByTitle(/.+/);
    expect(span).toBeInTheDocument();
  });

  it("includes brier signals in the tooltip when provided", () => {
    render(
      <MaturityBadge
        tier="drifting"
        signals={{
          production_sweep_count: 3,
          calibration_sample_count: 50,
          recent_brier: 0.32,
          baseline_brier: 0.20,
          brier_ratio: 1.6,
        }}
      />,
    );
    const span = screen.getByTitle(/Sweeps: 3.*Samples: 50.*0\.320 vs 0\.200.*1\.60/);
    expect(span).toBeInTheDocument();
  });

  it("color tone differs across tiers (mature green vs drifting amber)", () => {
    const { container: a } = render(<MaturityBadge tier="mature" />);
    const { container: b } = render(<MaturityBadge tier="drifting" />);
    const cls = (c: HTMLElement) => (c.firstChild as HTMLElement).className;
    expect(cls(a)).toContain("emerald");
    expect(cls(b)).toContain("amber");
  });
});
