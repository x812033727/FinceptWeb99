/**
 * Tests for Sidebar's mobile-drawer behaviour, grouped-nav rendering,
 * and prefetch wiring.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const { navigateMock, logoutMock, prefetchPageMock, locationMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  logoutMock: vi.fn(() => Promise.resolve()),
  prefetchPageMock: vi.fn(),
  locationMock: { pathname: "/dashboard" } as { pathname: string },
}));

vi.mock("react-router-dom", () => ({
  NavLink: ({
    to,
    onClick,
    onMouseEnter,
    onFocus,
    onTouchStart,
    className,
    children,
  }: {
    to: string;
    onClick?: () => void;
    onMouseEnter?: () => void;
    onFocus?: () => void;
    onTouchStart?: () => void;
    className?: string | ((s: { isActive: boolean }) => string);
    children: React.ReactNode;
  }) => (
    <a
      href={to}
      data-to={to}
      onClick={onClick}
      onMouseEnter={onMouseEnter}
      onFocus={onFocus}
      onTouchStart={onTouchStart}
      className={typeof className === "function" ? className({ isActive: false }) : className}
    >
      {children}
    </a>
  ),
  useNavigate: () => navigateMock,
  useLocation: () => locationMock,
}));

vi.mock("@/lib/auth", () => ({
  logout: logoutMock,
}));

const { authState } = vi.hoisted(() => ({
  authState: {
    user: null as { role: "viewer" | "analyst" | "admin"; email?: string } | null,
  },
}));
vi.mock("@/store/authStore", () => ({
  useAuthStore: <T,>(selector: (s: typeof authState) => T) => selector(authState),
}));

vi.mock("@/pageLoaders", () => ({
  prefetchPage: prefetchPageMock,
}));

import Sidebar from "./Sidebar";

function expandAllGroups() {
  // useCollapsible reads "1" → open, "0" → closed. Force every nav
  // group open so tests can assert against the full item list.
  ["markets", "workspace", "ai", "data", "system"].forEach((k) =>
    localStorage.setItem(`sidebar.group.${k}`, "1")
  );
}

function renderSidebar(open: boolean) {
  const onClose = vi.fn();
  const utils = render(<Sidebar open={open} onClose={onClose} />);
  return { ...utils, onClose };
}

beforeEach(() => {
  vi.clearAllMocks();
  authState.user = null;
  localStorage.clear();
  locationMock.pathname = "/dashboard";
  expandAllGroups();
});

describe("Sidebar drawer state", () => {
  it("aside is off-screen on `<lg:` when closed and on-screen when open", () => {
    const { container, rerender, onClose } = renderSidebar(false);
    const aside = container.querySelector("aside") as HTMLElement;
    expect(aside.className).toContain("-translate-x-full");

    rerender(<Sidebar open={true} onClose={onClose} />);
    expect(aside.className).toContain("translate-x-0");
    expect(aside.className).not.toContain("-translate-x-full");
  });

  it("backdrop is pointer-events-none when closed so it doesn't block clicks", () => {
    const { container } = renderSidebar(false);
    const backdrop = container.firstChild as HTMLElement;
    expect(backdrop.getAttribute("aria-hidden")).toBe("true");
    expect(backdrop.className).toContain("pointer-events-none");
    expect(backdrop.className).toContain("opacity-0");
  });

  it("backdrop becomes interactive and fully opaque when open", () => {
    const { container } = renderSidebar(true);
    const backdrop = container.firstChild as HTMLElement;
    expect(backdrop.className).toContain("opacity-100");
    expect(backdrop.className).not.toContain("pointer-events-none");
  });

  it("clicking the backdrop calls onClose", () => {
    const { container, onClose } = renderSidebar(true);
    const backdrop = container.firstChild as HTMLElement;
    fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("close-X button (mobile only — lg:hidden) calls onClose", () => {
    const { onClose } = renderSidebar(true);
    fireEvent.click(screen.getByLabelText("Close menu"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("Sidebar grouped nav", () => {
  it("renders the 16 base nav items (admin link hidden for non-admins)", () => {
    const { container } = renderSidebar(true);
    const links = container.querySelectorAll("a[data-to]");
    // 14 original + Lesson Library (PR-5b) + Strategy Compare (PR-5c).
    expect(links).toHaveLength(16);
    expect(container.querySelector('a[data-to="/admin"]')).toBeNull();
    expect(container.querySelector('a[data-to="/finmind"]')).not.toBeNull();
    expect(
      container.querySelector('a[data-to="/discussion/lessons"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('a[data-to="/discussion/compare"]'),
    ).not.toBeNull();
  });

  it("renders the admin link only for admin users", () => {
    authState.user = { role: "admin" };
    const { container } = renderSidebar(true);
    expect(container.querySelector('a[data-to="/admin"]')).not.toBeNull();
  });

  it("footer shows the signed-in email + role chip when available", () => {
    authState.user = { role: "analyst", email: "trader@example.com" };
    renderSidebar(true);
    expect(screen.getByText("trader@example.com")).toBeInTheDocument();
    expect(screen.getByText("analyst")).toBeInTheDocument();
  });

  it("renders 4 group headers (Finmind folded into System in W1)", () => {
    renderSidebar(true);
    expect(screen.getByText("Markets")).toBeInTheDocument();
    expect(screen.getByText("Workspace")).toBeInTheDocument();
    expect(screen.getByText("AI")).toBeInTheDocument();
    expect(screen.getByText("System")).toBeInTheDocument();
    // The former single-item "Data" group was merged into System.
    expect(screen.queryByText("Data")).toBeNull();
  });

  it("collapsing a group hides its items but keeps the header visible", () => {
    const { container } = renderSidebar(true);
    expect(container.querySelector('a[data-to="/portfolio"]')).not.toBeNull();
    fireEvent.click(screen.getByText("Workspace"));
    expect(container.querySelector('a[data-to="/portfolio"]')).toBeNull();
    expect(screen.getByText("Workspace")).toBeInTheDocument();
  });

  it("clicking a nav item closes the drawer", () => {
    const { container, onClose } = renderSidebar(true);
    fireEvent.click(container.querySelector('a[data-to="/portfolio"]') as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("hovering a nav item warms the destination route", () => {
    const { container } = renderSidebar(true);
    fireEvent.mouseEnter(container.querySelector('a[data-to="/screener"]') as HTMLElement);
    expect(prefetchPageMock).toHaveBeenCalledWith("/screener");
  });

  it("focusing a nav item also warms the route (keyboard nav)", () => {
    const { container } = renderSidebar(true);
    fireEvent.focus(container.querySelector('a[data-to="/watchlist"]') as HTMLElement);
    expect(prefetchPageMock).toHaveBeenCalledWith("/watchlist");
  });

  it("touchstart on a nav item warms the route (mobile tap-down)", () => {
    const { container } = renderSidebar(true);
    fireEvent.touchStart(container.querySelector('a[data-to="/ai"]') as HTMLElement);
    expect(prefetchPageMock).toHaveBeenCalledWith("/ai");
  });

  it("opening the drawer warm-prefetches every destination", () => {
    renderSidebar(true);
    expect(prefetchPageMock).toHaveBeenCalledWith("/dashboard");
    expect(prefetchPageMock).toHaveBeenCalledWith("/screener");
    expect(prefetchPageMock).toHaveBeenCalledWith("/finmind");
  });
});

describe("Sidebar logout", () => {
  it("calls logout(), closes the drawer, then redirects to /login", async () => {
    const { onClose } = renderSidebar(true);
    fireEvent.click(screen.getByText("Sign out"));
    await Promise.resolve();
    await Promise.resolve();
    expect(logoutMock).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(navigateMock).toHaveBeenCalledWith("/login");
  });
});
