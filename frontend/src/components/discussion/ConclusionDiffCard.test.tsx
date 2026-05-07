import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConclusionDiffCard } from "./ConclusionDiffCard";
import type { PostMortemDiff } from "@/types/discussion";

describe("ConclusionDiffCard", () => {
  it("renders nothing when diff is null/undefined", () => {
    const a = render(<ConclusionDiffCard diff={null} />);
    expect(a.container.firstChild).toBeNull();
    const b = render(<ConclusionDiffCard diff={undefined} />);
    expect(b.container.firstChild).toBeNull();
  });

  it("shows the no-change label when nothing structurally moved", () => {
    const diff: PostMortemDiff = {
      symbols_added: [],
      symbols_removed: [],
      confidence_changes: {},
      consensus_score_delta: 0,
      time_horizon_changed: false,
      reasoning_overlap: 0.95,
    };
    render(<ConclusionDiffCard diff={diff} />);
    expect(screen.getByText(/wording only/i)).toBeInTheDocument();
  });

  it("renders added and removed symbol chips", () => {
    const diff: PostMortemDiff = {
      symbols_added: ["2317", "0050"],
      symbols_removed: ["2454"],
      confidence_changes: {},
      consensus_score_delta: 0,
      time_horizon_changed: false,
      reasoning_overlap: 0.5,
    };
    render(<ConclusionDiffCard diff={diff} />);
    expect(screen.getByText("2317")).toBeInTheDocument();
    expect(screen.getByText("0050")).toBeInTheDocument();
    expect(screen.getByText("2454")).toBeInTheDocument();
  });

  it("renders confidence shifts with delta sign", () => {
    const diff: PostMortemDiff = {
      symbols_added: [],
      symbols_removed: [],
      confidence_changes: {
        "2330": { orig: 0.7, post: 0.5, delta: -0.2 },
        "2454": { orig: 0.4, post: 0.65, delta: 0.25 },
      },
      consensus_score_delta: 0,
      time_horizon_changed: false,
      reasoning_overlap: 0.5,
    };
    render(<ConclusionDiffCard diff={diff} />);
    expect(screen.getByText("2330")).toBeInTheDocument();
    expect(screen.getByText(/-0\.20/)).toBeInTheDocument();
    expect(screen.getByText("2454")).toBeInTheDocument();
    expect(screen.getByText(/\+0\.25/)).toBeInTheDocument();
  });

  it("colour-codes overlap by band: high→green, mid→amber, low→red", () => {
    const high: PostMortemDiff = {
      symbols_added: [], symbols_removed: [], confidence_changes: {},
      consensus_score_delta: 0, time_horizon_changed: false,
      reasoning_overlap: 0.85,
    };
    const { container: hc } = render(<ConclusionDiffCard diff={high} />);
    expect(hc.querySelector(".text-emerald-300")).toBeTruthy();

    const mid: PostMortemDiff = { ...high, reasoning_overlap: 0.55 };
    const { container: mc } = render(<ConclusionDiffCard diff={mid} />);
    expect(mc.querySelector(".text-amber-300")).toBeTruthy();

    const low: PostMortemDiff = { ...high, reasoning_overlap: 0.2 };
    const { container: lc } = render(<ConclusionDiffCard diff={low} />);
    expect(lc.querySelector(".text-red-300")).toBeTruthy();
  });

  it("flags horizon change when time_horizon_changed is true", () => {
    const diff: PostMortemDiff = {
      symbols_added: [], symbols_removed: [], confidence_changes: {},
      consensus_score_delta: 0, time_horizon_changed: true,
      reasoning_overlap: 0.5,
    };
    render(<ConclusionDiffCard diff={diff} />);
    expect(screen.getByText(/horizon changed/i)).toBeInTheDocument();
  });
});
