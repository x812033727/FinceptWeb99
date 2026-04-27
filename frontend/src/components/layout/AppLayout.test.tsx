/**
 * Integration tests for AppLayout.
 *
 * AppLayout owns three pieces of state that Sidebar can't see in
 * isolation:
 *   1. sidebarOpen — driven by the hamburger MenuButton.
 *   2. A useEffect that auto-closes the drawer when location.pathname
 *      changes (login redirect, search-bar selection, etc.).
 *   3. A useEffect that locks document.body.style.overflow while the
 *      drawer is open so the page behind doesn't drift under the
 *      user's thumb on mobile.
 *
 * Sidebar itself is stubbed so its props (open / onClose) are
 * inspectable via data attributes. This keeps the test focused on
 * AppLayout's orchestration; Sidebar's internal behaviour has its
 * own coverage in Sidebar.test.tsx.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// ── Hoisted mocks ─────────────────────────────────────────────────
const { navigateMock, locationState, prefetchPageMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  locationState: { pathname: "/dashboard" },
  prefetchPageMock: vi.fn(),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => navigateMock,
  useLocation: () => locationState,
  Outlet: () => <div data-testid="outlet">outlet content</div>,
}));

vi.mock("@/pageLoaders", () => ({
  prefetchPage: prefetchPageMock,
}));

// Sidebar stub: render a div that surfaces its props as data attrs
// so the integration test can assert on what AppLayout passed.
vi.mock("./Sidebar", () => ({
  default: ({ open, onClose }: { open: boolean; onClose: () => void }) => (
    <aside
      data-testid="sidebar-stub"
      data-open={String(open)}
      onClick={onClose}
    />
  ),
}));

// Update / status / notification widgets are independent — stub them
// so AppLayout's tree mounts without trying to talk to a real server
// or websocket.
vi.mock("./UpdateBadge", () => ({ default: () => null }));
vi.mock("@/hooks/useWebSocket", () => ({
  useAlertSocket: () => undefined,
  useWsConnected: () => true,
}));
// NotificationBell calls useNotificationStore() without a selector
// (destructures the whole state); other call sites might use the
// selector pattern. Support both shapes.
const _notificationState = {
  alerts: [],
  unreadCount: 0,
  markAllRead: () => {},
  dismiss: () => {},
  addAlert: () => {},
};
vi.mock("@/store/notificationStore", () => {
  const useNotificationStore = (selector?: (s: unknown) => unknown) =>
    selector ? selector(_notificationState) : _notificationState;
  // Zustand exposes getState as a method on the store function itself.
  (useNotificationStore as unknown as { getState: () => unknown }).getState = () => _notificationState;
  return { useNotificationStore };
});

vi.mock("@/store/themeStore", () => ({
  useThemeStore: () => ({ theme: "dark", toggle: () => {} }),
}));
vi.mock("@/lib/api", () => ({
  default: { get: vi.fn().mockResolvedValue({ data: [] }) },
}));

import AppLayout from "./AppLayout";

beforeEach(() => {
  vi.clearAllMocks();
  locationState.pathname = "/dashboard";
  document.body.style.overflow = "";
});

// ── MenuButton → drawer state ────────────────────────────────────

describe("AppLayout drawer state", () => {
  it("renders Sidebar with open=false initially", () => {
    render(<AppLayout />);
    expect(screen.getByTestId("sidebar-stub").getAttribute("data-open")).toBe("false");
  });

  it("clicking the hamburger MenuButton flips Sidebar open=true", () => {
    render(<AppLayout />);
    fireEvent.click(screen.getByLabelText("Open menu"));
    expect(screen.getByTestId("sidebar-stub").getAttribute("data-open")).toBe("true");
  });

  it("Sidebar's onClose callback (clicking the stub here) closes the drawer", () => {
    render(<AppLayout />);
    fireEvent.click(screen.getByLabelText("Open menu"));
    fireEvent.click(screen.getByTestId("sidebar-stub")); // stub forwards click to onClose
    expect(screen.getByTestId("sidebar-stub").getAttribute("data-open")).toBe("false");
  });
});

// ── Auto-close on route change ───────────────────────────────────

describe("AppLayout route-change auto-close", () => {
  it("closes the drawer when location.pathname changes", () => {
    const { rerender } = render(<AppLayout />);
    fireEvent.click(screen.getByLabelText("Open menu"));
    expect(screen.getByTestId("sidebar-stub").getAttribute("data-open")).toBe("true");

    // Simulate programmatic navigation: change useLocation's pathname
    // and force a re-render so the dependent useEffect runs.
    act(() => {
      locationState.pathname = "/portfolio";
    });
    rerender(<AppLayout />);

    expect(screen.getByTestId("sidebar-stub").getAttribute("data-open")).toBe("false");
  });

  it("does not flip the drawer state on re-renders that keep the same pathname", () => {
    const { rerender } = render(<AppLayout />);
    fireEvent.click(screen.getByLabelText("Open menu"));
    rerender(<AppLayout />);
    rerender(<AppLayout />);
    // Three renders, pathname unchanged → drawer stays open.
    expect(screen.getByTestId("sidebar-stub").getAttribute("data-open")).toBe("true");
  });
});

// ── Body scroll lock side effect ─────────────────────────────────

describe("AppLayout body-scroll lock", () => {
  it("sets body.style.overflow=hidden while drawer is open", () => {
    render(<AppLayout />);
    expect(document.body.style.overflow).toBe(""); // initial

    fireEvent.click(screen.getByLabelText("Open menu"));
    expect(document.body.style.overflow).toBe("hidden");
  });

  it("restores the previous overflow value when drawer closes", () => {
    document.body.style.overflow = "auto"; // user / app set this earlier
    render(<AppLayout />);
    fireEvent.click(screen.getByLabelText("Open menu"));
    expect(document.body.style.overflow).toBe("hidden");

    // Close via the stub (Sidebar's onClose callback).
    fireEvent.click(screen.getByTestId("sidebar-stub"));
    expect(document.body.style.overflow).toBe("auto");
  });

  it("does not touch body overflow on the initial render when drawer stays closed", () => {
    document.body.style.overflow = "scroll";
    render(<AppLayout />);
    // Drawer never opened → useEffect's `if (sidebarOpen)` branch
    // doesn't run, so the existing overflow value is preserved.
    expect(document.body.style.overflow).toBe("scroll");
  });
});

// ── Outlet renders inside main column ────────────────────────────

describe("AppLayout shell", () => {
  it("renders the routed page via Outlet alongside Sidebar + topbar", () => {
    render(<AppLayout />);
    expect(screen.getByTestId("outlet")).toBeInTheDocument();
    expect(screen.getByTestId("sidebar-stub")).toBeInTheDocument();
  });
});
