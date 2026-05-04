/**
 * Tests for Sidebar's mobile-drawer behaviour (#38) and prefetch
 * wiring (#41).
 *
 * Sidebar pulls in NavLink + useNavigate, the auth store, the logout
 * helper, and prefetchPage. We mock all four so the test stays
 * focused on the drawer/onClose/prefetch contract — not on the
 * router or any real network.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

// ── Mocks ─────────────────────────────────────────────────────────
// vi.hoisted lifts these declarations alongside the vi.mock calls so
// the factory functions below can close over them without tripping
// vitest's "before initialization" guard.
const { navigateMock, logoutMock, prefetchPageMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  logoutMock: vi.fn(() => Promise.resolve()),
  prefetchPageMock: vi.fn(),
}));

vi.mock("react-router-dom", () => ({
  // NavLink: render a plain anchor so onClick / onMouseEnter / etc.
  // bubble like the real component. The className-fn signature is
  // imitated so the active/inactive class branch still runs.
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
}));

vi.mock("@/lib/auth", () => ({
  logout: logoutMock,
}));

const { authState } = vi.hoisted(() => ({
  authState: { user: null as { role: "viewer" | "analyst" | "admin" } | null },
}));
vi.mock("@/store/authStore", () => ({
  useAuthStore: <T,>(selector: (s: typeof authState) => T) => selector(authState),
}));

vi.mock("@/pageLoaders", () => ({
  prefetchPage: prefetchPageMock,
}));

import Sidebar from "./Sidebar";

// ── helpers ───────────────────────────────────────────────────────
function renderSidebar(open: boolean) {
  const onClose = vi.fn();
  const utils = render(<Sidebar open={open} onClose={onClose} />);
  return { ...utils, onClose };
}

beforeEach(() => {
  vi.clearAllMocks();
  authState.user = null;
});

// ── Tests ─────────────────────────────────────────────────────────
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
    // Backdrop is the first <div> in the document — sibling of <aside>.
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

describe("Sidebar nav items", () => {
  it("renders the 14 base nav items (admin link hidden for non-admins)", () => {
    const { container } = renderSidebar(true);
    const links = container.querySelectorAll("a[data-to]");
    expect(links).toHaveLength(14);
    expect(container.querySelector('a[data-to="/admin"]')).toBeNull();
    expect(container.querySelector('a[data-to="/finmind"]')).not.toBeNull();
  });

  it("renders the admin link only for admin users", () => {
    authState.user = { role: "admin" };
    const { container } = renderSidebar(true);
    expect(container.querySelector('a[data-to="/admin"]')).not.toBeNull();
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
});

describe("Sidebar logout", () => {
  it("calls logout(), closes the drawer, then redirects to /login", async () => {
    const { onClose } = renderSidebar(true);
    fireEvent.click(screen.getByText("Sign out"));
    // logout() returns a promise — let it resolve before asserting on
    // the navigate call that fires after the await.
    await Promise.resolve();
    await Promise.resolve();
    expect(logoutMock).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(navigateMock).toHaveBeenCalledWith("/login");
  });
});
