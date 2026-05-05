/**
 * Tests for TabStrip — confirms that tabs 5+ collapse into the More
 * dropdown below md, that the active tab stays visible even when it
 * sits in the overflow zone, and that all tabs render inline at md+.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const { mediaState } = vi.hoisted(() => ({ mediaState: { matches: false } }));

vi.mock("@/hooks/useMediaQuery", () => ({
  useMediaQuery: () => mediaState.matches,
}));

import { TabStrip, type TabDef } from "./TabStrip";

function makeTabs(count: number, activeIdx: number): TabDef[] {
  return Array.from({ length: count }, (_, i) => ({
    key: `t${i}`,
    label: `Tab ${i}`,
    active: i === activeIdx,
    onClick: vi.fn(),
  }));
}

beforeEach(() => {
  mediaState.matches = false;
});

describe("TabStrip — narrow viewport (<md)", () => {
  it("renders all tabs inline when total <= visible limit (4)", () => {
    const tabs = makeTabs(4, 0);
    render(<TabStrip tabs={tabs} />);
    expect(screen.getByRole("button", { name: "Tab 0" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tab 3" })).toBeInTheDocument();
    expect(screen.queryByLabelText("More tabs")).toBeNull();
  });

  it("shows first 4 tabs + More dropdown when total > 4 and active is in first 4", () => {
    const tabs = makeTabs(8, 0);
    render(<TabStrip tabs={tabs} />);
    expect(screen.getByRole("button", { name: "Tab 0" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tab 3" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Tab 4" })).toBeNull();
    expect(screen.getByLabelText("More tabs")).toBeInTheDocument();
  });

  it("swaps the active tab into the visible row when active sits in overflow", () => {
    const tabs = makeTabs(8, 6);
    render(<TabStrip tabs={tabs} />);
    expect(screen.getByRole("button", { name: "Tab 0" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tab 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tab 2" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tab 6" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Tab 3" })).toBeNull();
    expect(screen.getByLabelText("More tabs")).toBeInTheDocument();
  });

  it("clicking a visible tab fires its onClick", () => {
    const tabs = makeTabs(8, 0);
    render(<TabStrip tabs={tabs} />);
    fireEvent.click(screen.getByRole("button", { name: "Tab 2" }));
    expect(tabs[2].onClick).toHaveBeenCalledTimes(1);
  });
});

describe("TabStrip — md+ viewport", () => {
  it("renders all tabs inline regardless of count", () => {
    mediaState.matches = true;
    const tabs = makeTabs(8, 0);
    render(<TabStrip tabs={tabs} />);
    expect(screen.getByRole("button", { name: "Tab 0" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tab 7" })).toBeInTheDocument();
    expect(screen.queryByLabelText("More tabs")).toBeNull();
  });
});
