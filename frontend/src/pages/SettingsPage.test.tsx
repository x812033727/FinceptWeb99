/**
 * Tests for SettingsPage's new Preferences section + admin shortcut.
 * Existing Profile / Password / API-Keys behaviour is unchanged from
 * pre-PR-2 and exercised at the API layer.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { themeToggleMock, changeLanguageMock } = vi.hoisted(() => ({
  themeToggleMock: vi.fn(),
  changeLanguageMock: vi.fn(),
}));

const { authState } = vi.hoisted(() => ({
  authState: { user: { role: "viewer" } as { role: "viewer" | "analyst" | "admin" } | null },
}));
vi.mock("@/store/authStore", () => ({
  useAuthStore: <T,>(selector: (s: typeof authState) => T) => selector(authState),
}));

const { themeState } = vi.hoisted(() => ({
  themeState: { theme: "dark" as "dark" | "light", toggle: () => {} },
}));
vi.mock("@/store/themeStore", () => ({
  useThemeStore: () => ({ theme: themeState.theme, toggle: themeToggleMock }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string) => k,
    i18n: { language: "en", changeLanguage: changeLanguageMock },
  }),
}));

vi.mock("@/lib/api", () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: [] }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    post: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

vi.mock("@/components/admin/UsageCard", () => ({
  UsageCard: () => <div data-testid="usage-card" />,
}));

import SettingsPage from "./SettingsPage";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  authState.user = { role: "viewer" };
  themeState.theme = "dark";
});

describe("SettingsPage Preferences", () => {
  it("renders the theme + language preference controls", () => {
    renderPage();
    expect(screen.getByText("settings.preferences.theme")).toBeInTheDocument();
    expect(screen.getByText("settings.preferences.language")).toBeInTheDocument();
  });

  it("toggling the theme calls themeStore.toggle", () => {
    renderPage();
    fireEvent.click(
      screen.getByRole("button", { name: /switch_to_light|switch_to_dark/ })
    );
    expect(themeToggleMock).toHaveBeenCalledTimes(1);
  });

  it("switching language calls i18n.changeLanguage('zh-TW') from English", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /切換為繁體中文/ }));
    expect(changeLanguageMock).toHaveBeenCalledWith("zh-TW");
  });
});

describe("SettingsPage admin shortcut", () => {
  it("does NOT render the admin link for non-admin users", () => {
    renderPage();
    expect(screen.queryByText(/settings\.admin_link/)).toBeNull();
  });

  it("renders the admin link for admin users", () => {
    authState.user = { role: "admin" };
    renderPage();
    const link = screen.getByText(/settings\.admin_link/).closest("a");
    expect(link?.getAttribute("href")).toBe("/admin");
  });
});
