import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { Skeleton, PageSkeleton } from "./Skeleton";

describe("Skeleton", () => {
  it("merges caller className with the pulse + colour defaults", () => {
    const { container } = render(<Skeleton className="h-7 w-40" />);
    const div = container.firstChild as HTMLElement;
    expect(div).toBeTruthy();
    expect(div.className).toContain("animate-pulse");
    expect(div.className).toContain("bg-muted/60");
    expect(div.className).toContain("rounded");
    expect(div.className).toContain("h-7");
    expect(div.className).toContain("w-40");
  });

  it("is hidden from assistive tech (decorative placeholder)", () => {
    const { container } = render(<Skeleton />);
    const div = container.firstChild as HTMLElement;
    expect(div.getAttribute("aria-hidden")).toBe("true");
  });
});

describe("PageSkeleton", () => {
  it("renders the canonical page shape — title bar, subtitle, 2x4 cards, body block", () => {
    const { container } = render(<PageSkeleton />);
    // 1 title bar + 1 subtitle + 4 cards + 1 body = 7 skeleton blocks total.
    const blocks = container.querySelectorAll('[aria-hidden="true"]');
    expect(blocks).toHaveLength(7);
  });

  it("uses the same outer padding as real pages so the hand-off is seamless", () => {
    const { container } = render(<PageSkeleton />);
    const outer = container.firstChild as HTMLElement;
    expect(outer.className).toContain("p-4");
    expect(outer.className).toContain("sm:p-6");
  });
});
