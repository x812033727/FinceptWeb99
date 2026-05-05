/**
 * Tests for CommandPalette — Cmd+K listener, page navigation, and
 * the action sub-section. We don't exercise cmdk's fuzzy-match
 * itself (covered upstream); we just confirm our wiring.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, act } from "@testing-library/react";

const { navigateMock, logoutMock, themeToggleMock, prefetchPageMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  logoutMock: vi.fn(() => Promise.resolve()),
  themeToggleMock: vi.fn(),
  prefetchPageMock: vi.fn(),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => navigateMock,
}));

vi.mock("@/lib/auth", () => ({
  logout: logoutMock,
}));

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn(() => Promise.resolve({ data: [] })) },
}));

vi.mock("@/pageLoaders", () => ({
  prefetchPage: prefetchPageMock,
}));

const { authState } = vi.hoisted(() => ({
  authState: { user: null as { role: "viewer" | "analyst" | "admin" } | null },
}));
vi.mock("@/store/authStore", () => ({
  useAuthStore: <T,>(selector: (s: typeof authState) => T) => selector(authState),
}));

vi.mock("@/store/themeStore", () => ({
  useThemeStore: <T,>(selector: (s: { theme: "dark" | "light"; toggle: () => void }) => T) =>
    selector({ theme: "dark", toggle: themeToggleMock }),
}));

import { CommandPaletteProvider } from "./CommandPalette";
import { useCommandPalette } from "@/hooks/useCommandPalette";

function Probe() {
  const { open, setOpen } = useCommandPalette();
  return (
    <div>
      <span data-testid="state">{open ? "open" : "closed"}</span>
      <button onClick={() => setOpen(true)}>force-open</button>
    </div>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  authState.user = { role: "analyst" };
});

describe("CommandPalette", () => {
  it("Cmd+K toggles the palette open", () => {
    render(
      <CommandPaletteProvider>
        <Probe />
      </CommandPaletteProvider>
    );
    expect(screen.getByTestId("state").textContent).toBe("closed");
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }));
    });
    expect(screen.getByTestId("state").textContent).toBe("open");
  });

  it("Ctrl+K also toggles (Windows/Linux)", () => {
    render(
      <CommandPaletteProvider>
        <Probe />
      </CommandPaletteProvider>
    );
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "K", ctrlKey: true }));
    });
    expect(screen.getByTestId("state").textContent).toBe("open");
  });

  it("renders pages section once opened", () => {
    render(
      <CommandPaletteProvider>
        <Probe />
      </CommandPaletteProvider>
    );
    fireEvent.click(screen.getByText("force-open"));
    expect(screen.getByText("Pages")).toBeInTheDocument();
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Portfolio")).toBeInTheDocument();
  });

  it("hides admin-only routes for non-admin users", () => {
    authState.user = { role: "viewer" };
    render(
      <CommandPaletteProvider>
        <Probe />
      </CommandPaletteProvider>
    );
    fireEvent.click(screen.getByText("force-open"));
    expect(screen.queryByText("Admin")).toBeNull();
  });

  it("shows the admin route for admin users", () => {
    authState.user = { role: "admin" };
    render(
      <CommandPaletteProvider>
        <Probe />
      </CommandPaletteProvider>
    );
    fireEvent.click(screen.getByText("force-open"));
    expect(screen.getByText("Admin")).toBeInTheDocument();
  });

  it("selecting a page item navigates and closes the dialog", () => {
    render(
      <CommandPaletteProvider>
        <Probe />
      </CommandPaletteProvider>
    );
    fireEvent.click(screen.getByText("force-open"));
    fireEvent.click(screen.getByText("Portfolio"));
    expect(navigateMock).toHaveBeenCalledWith("/portfolio");
    expect(screen.getByTestId("state").textContent).toBe("closed");
  });

  it("'Toggle theme' action triggers themeStore.toggle and closes", () => {
    render(
      <CommandPaletteProvider>
        <Probe />
      </CommandPaletteProvider>
    );
    fireEvent.click(screen.getByText("force-open"));
    fireEvent.click(screen.getByText("Toggle theme"));
    expect(themeToggleMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("state").textContent).toBe("closed");
  });
});
