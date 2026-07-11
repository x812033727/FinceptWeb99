/**
 * Tests for SettingsPage's new Preferences section + admin shortcut.
 * Existing Profile / Password / API-Keys behaviour is unchanged from
 * pre-PR-2 and exercised at the API layer.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

import api from "@/lib/api";
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

// ── Web Push toggle (D3 瀏覽器推播) ────────────────────────────────

function makeFakeSubscription(endpoint = "https://push.example.com/sub/1") {
  return {
    endpoint,
    toJSON: () => ({ endpoint, keys: { p256dh: "BKey", auth: "Auth" } }),
    unsubscribe: vi.fn().mockResolvedValue(true),
  };
}

function stubPushEnv({
  permission = "default" as NotificationPermission,
  requestPermissionResult = "granted" as NotificationPermission,
  subscription = null as ReturnType<typeof makeFakeSubscription> | null,
} = {}) {
  const pushManager = {
    getSubscription: vi.fn().mockResolvedValue(subscription),
    subscribe: vi.fn().mockResolvedValue(makeFakeSubscription()),
  };
  vi.stubGlobal("Notification", {
    permission,
    requestPermission: vi.fn().mockResolvedValue(requestPermissionResult),
  });
  vi.stubGlobal("PushManager", class {});
  Object.defineProperty(window.navigator, "serviceWorker", {
    value: { ready: Promise.resolve({ pushManager }) },
    configurable: true,
  });
  return { pushManager };
}

describe("SettingsPage web push toggle", () => {
  beforeEach(() => {
    // Route-aware GET: the vapid key fetch must succeed while the
    // profile / api-keys queries keep their empty defaults.
    vi.mocked(api.get).mockImplementation((url: string) =>
      url === "/notifications/vapid-public-key"
        ? Promise.resolve({ data: { configured: true, public_key: "QUJDRA" } })
        : Promise.resolve({ data: [] })
    );
  });

  afterEach(() => {
    vi.mocked(api.get).mockResolvedValue({ data: [] });
    vi.unstubAllGlobals();
    // @ts-expect-error test cleanup of the defined property
    delete window.navigator.serviceWorker;
  });

  it("shows the unsupported state in a bare jsdom environment", async () => {
    renderPage();
    expect(
      await screen.findByText("settings.notifications.unsupported")
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /settings\.notifications\.enable/ })
    ).toBeNull();
  });

  it("enables push: permission → subscribe → POST, then flips to disable", async () => {
    const { pushManager } = stubPushEnv();
    renderPage();

    const enableBtn = await screen.findByRole("button", {
      name: /settings\.notifications\.enable/,
    });
    fireEvent.click(enableBtn);

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/notifications/push-subscribe", {
        endpoint: "https://push.example.com/sub/1",
        keys: { p256dh: "BKey", auth: "Auth" },
        user_agent: expect.any(String),
      })
    );
    expect(pushManager.subscribe).toHaveBeenCalledWith(
      expect.objectContaining({ userVisibleOnly: true })
    );
    expect(
      await screen.findByRole("button", { name: /settings\.notifications\.disable/ })
    ).toBeInTheDocument();
  });

  it("shows the denied state when the permission prompt is refused", async () => {
    stubPushEnv({ requestPermissionResult: "denied" });
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /settings\.notifications\.enable/ })
    );

    expect(
      await screen.findByText("settings.notifications.denied")
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /settings\.notifications/ })
    ).toBeNull();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("shows denied immediately when notifications are already blocked", async () => {
    stubPushEnv({ permission: "denied" });
    renderPage();
    expect(
      await screen.findByText("settings.notifications.denied")
    ).toBeInTheDocument();
  });

  it("disables push: unsubscribe + DELETE, then flips back to enable", async () => {
    const sub = makeFakeSubscription();
    stubPushEnv({ permission: "granted", subscription: sub });
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /settings\.notifications\.disable/ })
    );

    await waitFor(() =>
      expect(api.delete).toHaveBeenCalledWith("/notifications/push-subscribe", {
        data: { endpoint: "https://push.example.com/sub/1" },
      })
    );
    expect(sub.unsubscribe).toHaveBeenCalledTimes(1);
    expect(
      await screen.findByRole("button", { name: /settings\.notifications\.enable/ })
    ).toBeInTheDocument();
  });

  it("surfaces the unconfigured state when the server lacks VAPID keys", async () => {
    stubPushEnv();
    vi.mocked(api.get).mockImplementation((url: string) =>
      url === "/notifications/vapid-public-key"
        ? Promise.resolve({ data: { configured: false, public_key: null } })
        : Promise.resolve({ data: [] })
    );
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /settings\.notifications\.enable/ })
    );

    expect(
      await screen.findByText("settings.notifications.unconfigured")
    ).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });
});
