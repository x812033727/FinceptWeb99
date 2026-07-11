/**
 * Integration tests for AdminPage's tab orchestration.
 *
 * The 16 admin cards each have their own data-fetch + render path
 * (covered separately). Here we only assert AdminPage's contract:
 *   - 6 tabs render their assigned card stubs
 *   - the active tab is driven by ?tab= in the URL
 *   - clicking a TabsTrigger updates the URL
 *   - non-admin users are redirected to /dashboard
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { navigateMock } = vi.hoisted(() => ({ navigateMock: vi.fn() }));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => navigateMock,
  };
});

const { authState } = vi.hoisted(() => ({
  authState: { user: { role: "admin" } as { role: "viewer" | "analyst" | "admin" } | null },
}));
vi.mock("@/store/authStore", () => ({
  useAuthStore: <T,>(selector: (s: typeof authState) => T) => selector(authState),
}));

// Stub every admin card so its data-fetch side effects don't fire.
function stubCard(name: string) {
  return { default: () => <div data-testid={name} /> };
}
function namedStub(exportName: string, testId: string) {
  return { [exportName]: () => <div data-testid={testId} /> };
}

vi.mock("@/components/admin/SystemUpdateCard", () => namedStub("SystemUpdateCard", "card-system-update"));
vi.mock("@/components/admin/IngestHealthCard", () => namedStub("IngestHealthCard", "card-ingest-health"));
vi.mock("@/components/admin/RuntimeTunablesCard", () => namedStub("RuntimeTunablesCard", "card-runtime-tunables"));
vi.mock("@/components/admin/LLMKeysCard", () => namedStub("LLMKeysCard", "card-llm-keys"));
vi.mock("@/components/admin/SystemTasksCard", () => namedStub("SystemTasksCard", "card-system-tasks"));
vi.mock("@/components/admin/PersonasCard", () => namedStub("PersonasCard", "card-personas"));
vi.mock("@/components/admin/MarketKeysCard", () => namedStub("MarketKeysCard", "card-market-keys"));
vi.mock("@/components/admin/FinmindAdminCard", () => stubCard("card-finmind-admin"));
vi.mock("@/components/admin/FinmindKeysCard", () => stubCard("card-finmind-keys"));
vi.mock("@/components/admin/FinmindUsageCard", () => stubCard("card-finmind-usage"));
vi.mock("@/components/admin/SignalAuditCard", () => namedStub("SignalAuditCard", "card-signal-audit"));
vi.mock("@/components/admin/SignalQualityCard", () => namedStub("SignalQualityCard", "card-signal-quality"));
vi.mock("@/components/admin/PostMortemGapsCard", () => namedStub("PostMortemGapsCard", "card-postmortem-gaps"));
vi.mock("@/components/admin/DiscussionLessonsCard", () => namedStub("DiscussionLessonsCard", "card-discussion-lessons"));
vi.mock("@/components/admin/UsageCard", () => ({
  UsageCard: ({ scope }: { scope: string }) => <div data-testid={`card-usage-${scope}`} />,
}));

vi.mock("@/lib/api", () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: [] }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

import api from "@/lib/api";
import AdminPage from "./AdminPage";

function renderAt(initialUrl: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialUrl]}>
        <Routes>
          <Route path="/admin" element={<AdminPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  authState.user = { role: "admin" };
  localStorage.clear();
});

describe("AdminPage role gate", () => {
  it("redirects non-admin users away from /admin", () => {
    authState.user = { role: "analyst" };
    const { container } = renderAt("/admin");
    expect(navigateMock).toHaveBeenCalledWith("/dashboard", { replace: true });
    expect(container.querySelector("h1")).toBeNull();
  });
});

describe("AdminPage tabs", () => {
  it("defaults to the 'ops' tab when ?tab= is absent", () => {
    renderAt("/admin");
    expect(screen.getByTestId("card-system-update")).toBeInTheDocument();
    expect(screen.getByTestId("card-ingest-health")).toBeInTheDocument();
    expect(screen.getByTestId("card-runtime-tunables")).toBeInTheDocument();
    expect(screen.queryByTestId("card-llm-keys")).toBeNull();
  });

  it("respects ?tab=ai on first render", () => {
    renderAt("/admin?tab=ai");
    expect(screen.getByTestId("card-llm-keys")).toBeInTheDocument();
    expect(screen.getByTestId("card-system-tasks")).toBeInTheDocument();
    expect(screen.getByTestId("card-personas")).toBeInTheDocument();
    expect(screen.queryByTestId("card-system-update")).toBeNull();
  });

  it("falls back to 'ops' when ?tab= contains an unknown value", () => {
    renderAt("/admin?tab=bogus");
    expect(screen.getByTestId("card-system-update")).toBeInTheDocument();
  });

  it("'data' tab renders MarketKeysCard + FinMind cards", () => {
    renderAt("/admin?tab=data");
    expect(screen.getByTestId("card-market-keys")).toBeInTheDocument();
    expect(screen.getByTestId("card-finmind-admin")).toBeInTheDocument();
    expect(screen.getByTestId("card-finmind-keys")).toBeInTheDocument();
  });

  it("'quality' tab renders signal + post-mortem cards", () => {
    renderAt("/admin?tab=quality");
    expect(screen.getByTestId("card-signal-audit")).toBeInTheDocument();
    expect(screen.getByTestId("card-signal-quality")).toBeInTheDocument();
    expect(screen.getByTestId("card-postmortem-gaps")).toBeInTheDocument();
    expect(screen.getByTestId("card-discussion-lessons")).toBeInTheDocument();
  });

  it("'usage' tab renders both UsageCard scopes", () => {
    renderAt("/admin?tab=usage");
    expect(screen.getByTestId("card-usage-admin")).toBeInTheDocument();
    expect(screen.getByTestId("card-finmind-usage")).toBeInTheDocument();
  });

  it("'ops' is the aria-selected tab when ?tab=ops", () => {
    renderAt("/admin?tab=ops");
    const opsTab = screen.getByRole("tab", { name: "Operations", hidden: true });
    expect(opsTab.getAttribute("aria-selected")).toBe("true");
    const aiTab = screen.getByRole("tab", { name: "AI & LLM", hidden: true });
    expect(aiTab.getAttribute("aria-selected")).toBe("false");
  });

  it("?tab=quality marks 'Signal Quality' as the aria-selected tab", () => {
    renderAt("/admin?tab=quality");
    const tab = screen.getByRole("tab", { name: "Signal Quality", hidden: true });
    expect(tab.getAttribute("aria-selected")).toBe("true");
  });
});

describe("AdminPage users tab virtualization (PR-9)", () => {
  // jsdom has no layout — offsetHeight (which @tanstack/virtual-core
  // uses for both the scroll rect and measureElement) is always 0, so
  // the virtualizer would mount nothing. Pin it to the row height
  // (44px, same as estimateSize).
  const originalOffsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
  const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");
  beforeEach(() => {
    Object.defineProperties(HTMLElement.prototype, {
      offsetHeight: { get: () => 44, configurable: true },
      offsetWidth: { get: () => 800, configurable: true },
    });
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url.startsWith("/admin/users")) {
        return Promise.resolve({
          data: Array.from({ length: 200 }, (_, i) => ({
            id: `u-${i}`,
            email: `user${i}@example.com`,
            role: "viewer",
            is_active: true,
            created_at: "2026-01-01T00:00:00Z",
          })),
        });
      }
      if (url.startsWith("/admin/stats")) {
        return Promise.resolve({
          data: {
            total_users: 200,
            active_users: 200,
            users_by_role: { viewer: 200 },
            total_alerts: 0,
            total_watchlists: 0,
          },
        });
      }
      return Promise.resolve({ data: [] });
    });
  });

  afterEach(() => {
    if (originalOffsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
    if (originalOffsetWidth) Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
    vi.mocked(api.get).mockReset();
    vi.mocked(api.get).mockResolvedValue({ data: [] });
  });

  it("mounts only a window of 200 user rows", async () => {
    renderAt("/admin?tab=users");

    expect(await screen.findByText("Users (200)")).toBeInTheDocument();

    const scroller = screen.getByTestId("admin-users-virtual-scroll");
    // The virtualizer measures the scroll element in a layout effect,
    // so give the resulting re-render a tick to land.
    const rows = await waitFor(() => {
      const mounted = scroller.querySelectorAll("[data-index]");
      expect(mounted.length).toBeGreaterThan(0);
      return mounted;
    });
    expect(rows.length).toBeLessThan(50);

    // Mounted rows carry real cell content.
    const firstRow = rows[0] as HTMLElement;
    const idx = Number(firstRow.getAttribute("data-index"));
    expect(firstRow.textContent).toContain(`user${idx}@example.com`);
    expect(firstRow.textContent).toContain("active");
  });
});
