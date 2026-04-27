/**
 * Tests for UpdateBadge.
 *
 * useVersion is mocked at the module seam so each test feeds the
 * badge a controlled version-status payload — keeps the test
 * independent of the network and TanStack Query.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const { useVersionMock } = vi.hoisted(() => ({
  useVersionMock: vi.fn(),
}));

vi.mock("@/hooks/useVersion", () => ({
  useVersion: useVersionMock,
}));

import UpdateBadge from "./UpdateBadge";

function setVersion(data: unknown, isError = false) {
  useVersionMock.mockReturnValue({ data, isError });
}

// ── Render gate ──────────────────────────────────────────────────

describe("UpdateBadge — render gate", () => {
  it("renders a link to the release when an update is available", () => {
    setVersion({
      current: "0.2.2",
      latest: "0.3.0",
      update_available: true,
      html_url: "https://github.com/x812033727/FinceptWeb/releases/tag/v0.3.0",
      published_at: "2024-04-01T00:00:00Z",
    });
    render(<UpdateBadge />);
    const link = screen.getByRole("link");
    expect(link).toHaveTextContent("v0.3.0 available");
    expect(link.getAttribute("href")).toBe(
      "https://github.com/x812033727/FinceptWeb/releases/tag/v0.3.0",
    );
  });

  it("renders nothing when update_available is false", () => {
    setVersion({
      current: "0.3.0", latest: "0.3.0", update_available: false,
      html_url: "", published_at: "",
    });
    const { container } = render(<UpdateBadge />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing while the query is still loading (data undefined)", () => {
    setVersion(undefined);
    const { container } = render(<UpdateBadge />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when the query is in an error state", () => {
    setVersion(undefined, true);
    const { container } = render(<UpdateBadge />);
    expect(container.firstChild).toBeNull();
  });
});

// ── Anchor attributes ────────────────────────────────────────────

describe("UpdateBadge — anchor safety + UX", () => {
  it("opens the release in a new tab and uses noopener noreferrer", () => {
    setVersion({
      current: "0.2.2", latest: "0.3.0", update_available: true,
      html_url: "https://example.com/release", published_at: "2024-04-01",
    });
    render(<UpdateBadge />);
    const link = screen.getByRole("link");
    expect(link.getAttribute("target")).toBe("_blank");
    // tabnabbing prevention — required when target=_blank with an
    // external href.
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("rel")).toContain("noreferrer");
  });

  it("falls back to href='#' when html_url is missing (still safe to click)", () => {
    setVersion({
      current: "0.2.2", latest: "0.3.0", update_available: true,
      html_url: "", published_at: "2024-04-01",
    });
    render(<UpdateBadge />);
    expect(screen.getByRole("link").getAttribute("href")).toBe("#");
  });

  it("includes the publish date in the tooltip title", () => {
    setVersion({
      current: "0.2.2", latest: "0.3.0", update_available: true,
      html_url: "https://example.com/r", published_at: "2024-04-01",
    });
    render(<UpdateBadge />);
    const link = screen.getByRole("link");
    expect(link.getAttribute("title")).toContain("2024-04-01");
  });

  it("uses 'unknown' in the tooltip when published_at is empty", () => {
    setVersion({
      current: "0.2.2", latest: "0.3.0", update_available: true,
      html_url: "https://example.com/r", published_at: "",
    });
    render(<UpdateBadge />);
    expect(screen.getByRole("link").getAttribute("title")).toContain("unknown");
  });
});

// ── RWD ──────────────────────────────────────────────────────────

describe("UpdateBadge — RWD (PR #38)", () => {
  it("hides on `<sm:` viewports and shows on `sm:+` (admin-y, low priority on phones)", () => {
    setVersion({
      current: "0.2.2", latest: "0.3.0", update_available: true,
      html_url: "https://example.com/r", published_at: "2024-04-01",
    });
    render(<UpdateBadge />);
    const link = screen.getByRole("link");
    expect(link.className).toContain("hidden");
    expect(link.className).toContain("sm:inline-block");
  });
});
