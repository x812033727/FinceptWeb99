/**
 * Tests for BottomNav — five-tab mobile primary nav. Covers active-tab
 * matching across drill-in routes and the "More" button callback.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const { navigateMock, prefetchPageMock, locationMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  prefetchPageMock: vi.fn(),
  locationMock: { pathname: "/dashboard" } as { pathname: string },
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => navigateMock,
  useLocation: () => locationMock,
}));

vi.mock("@/pageLoaders", () => ({
  prefetchPage: prefetchPageMock,
}));

import BottomNav from "./BottomNav";

beforeEach(() => {
  vi.clearAllMocks();
  locationMock.pathname = "/dashboard";
});

describe("BottomNav", () => {
  it("renders five tabs", () => {
    render(<BottomNav onOpenMore={vi.fn()} />);
    expect(screen.getByText("Home")).toBeInTheDocument();
    expect(screen.getByText("Markets")).toBeInTheDocument();
    expect(screen.getByText("Portfolio")).toBeInTheDocument();
    expect(screen.getByText("AI")).toBeInTheDocument();
    expect(screen.getByText("More")).toBeInTheDocument();
  });

  it("'Markets' tab is active when on /stock/<MARKET>/<SYMBOL>", () => {
    locationMock.pathname = "/stock/US/AAPL";
    render(<BottomNav onOpenMore={vi.fn()} />);
    const marketsBtn = screen.getByText("Markets").closest("button") as HTMLElement;
    expect(marketsBtn.getAttribute("aria-current")).toBe("page");
  });

  it("'Portfolio' tab is active when on /watchlist or /alerts", () => {
    locationMock.pathname = "/alerts";
    render(<BottomNav onOpenMore={vi.fn()} />);
    const portfolioBtn = screen.getByText("Portfolio").closest("button") as HTMLElement;
    expect(portfolioBtn.getAttribute("aria-current")).toBe("page");
  });

  it("'AI' tab is active when on /discussion", () => {
    locationMock.pathname = "/discussion";
    render(<BottomNav onOpenMore={vi.fn()} />);
    const aiBtn = screen.getByText("AI").closest("button") as HTMLElement;
    expect(aiBtn.getAttribute("aria-current")).toBe("page");
  });

  it("renders the active-tab accent bar only on the active tab", () => {
    locationMock.pathname = "/dashboard";
    render(<BottomNav onOpenMore={vi.fn()} />);
    const homeBtn = screen.getByText("Home").closest("button") as HTMLElement;
    const marketsBtn = screen.getByText("Markets").closest("button") as HTMLElement;
    // The accent bar is a bg-primary hairline rendered inside the active tab.
    expect(homeBtn.querySelector(".bg-primary")).not.toBeNull();
    expect(marketsBtn.querySelector(".bg-primary")).toBeNull();
  });

  it("clicking a tab navigates to its route", () => {
    render(<BottomNav onOpenMore={vi.fn()} />);
    fireEvent.click(screen.getByText("Portfolio"));
    expect(navigateMock).toHaveBeenCalledWith("/portfolio");
  });

  it("touchstart on a tab warms its route", () => {
    render(<BottomNav onOpenMore={vi.fn()} />);
    fireEvent.touchStart(screen.getByText("AI"));
    expect(prefetchPageMock).toHaveBeenCalledWith("/ai");
  });

  it("clicking 'More' invokes the onOpenMore callback (drawer trigger)", () => {
    const onOpenMore = vi.fn();
    render(<BottomNav onOpenMore={onOpenMore} />);
    fireEvent.click(screen.getByText("More"));
    expect(onOpenMore).toHaveBeenCalledTimes(1);
  });

  it("hides on lg+ via the lg:hidden class", () => {
    const { container } = render(<BottomNav onOpenMore={vi.fn()} />);
    const nav = container.querySelector("nav") as HTMLElement;
    expect(nav.className).toContain("lg:hidden");
  });
});
