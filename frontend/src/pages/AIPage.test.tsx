/**
 * Tests for AIPage layout polish (PR-F): persona search, collapsible
 * persona groups, and the inline Tools ON/OFF chip that replaces the
 * buried dropdown checkbox.
 *
 * These tests focus on the layout interactions only; the chat /
 * streaming flow is covered separately at the API + composer level.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { authState } = vi.hoisted(() => ({
  authState: {
    token: "tok",
    user: { role: "analyst" } as {
      role: "viewer" | "analyst" | "admin";
    } | null,
  },
}));
vi.mock("@/store/authStore", () => ({
  useAuthStore: <T,>(selector: (s: typeof authState) => T) =>
    selector(authState),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string, opts?: Record<string, unknown>) => {
      if (opts && typeof opts === "object") {
        // Render templated strings inline so `screen.getByText`
        // can find them — e.g. "ai.quota_remaining_short" with
        // remaining=42 becomes "ai.quota_remaining_short:42".
        const tail = Object.values(opts).join(",");
        return `${k}:${tail}`;
      }
      return k;
    },
    i18n: { language: "en", exists: () => false },
  }),
}));

const mockApiGet = vi.fn();
vi.mock("@/lib/api", () => ({
  default: { get: (...args: unknown[]) => mockApiGet(...args) },
  notifyRateLimited: vi.fn(),
}));

vi.mock("@/components/ai/MessageBubble", () => ({
  MessageBubble: () => null,
}));
vi.mock("@/components/ai/ChatComposer", () => ({
  ChatComposer: () => null,
}));

import AIPage from "./AIPage";

const AGENTS = [
  // First entry becomes the default `selectedAgent` via AIPage's
  // useEffect on first agent fetch. Tests rely on `market_analyst`
  // (functional group) being the default so the functional group
  // opens by default and the value group stays collapsed.
  { id: "market_analyst", name: "Market Analyst", description: "Functional", default_provider: "openai" },
  { id: "buffett", name: "Warren Buffett", description: "Value investor", default_provider: "openai" },
  { id: "graham",  name: "Benjamin Graham", description: "Margin of safety", default_provider: "openai" },
  { id: "lynch",   name: "Peter Lynch",     description: "Growth & GARP",     default_provider: "anthropic" },
  { id: "dalio",   name: "Ray Dalio",       description: "All weather macro", default_provider: "openai" },
  { id: "simons",  name: "Jim Simons",      description: "Quantitative",      default_provider: "anthropic" },
];

beforeEach(() => {
  localStorage.clear();
  mockApiGet.mockReset();
  mockApiGet.mockImplementation((url: string) => {
    if (url === "/ai/agents") return Promise.resolve({ data: AGENTS });
    if (url === "/auth/me") return Promise.resolve({ data: { ai_requests_remaining: 42 } });
    return Promise.resolve({ data: {} });
  });
});

afterEach(() => {
  document.body.innerHTML = "";
});

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AIPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AIPage layout polish", () => {
  it("renders the persona search input with i18n placeholder", async () => {
    renderPage();
    const inputs = await screen.findAllByPlaceholderText(
      "ai.persona_search_placeholder",
    );
    // Desktop sidebar + mobile sheet both render the list, but the
    // sheet is closed on initial render so only one input mounts.
    expect(inputs.length).toBeGreaterThanOrEqual(1);
  });

  it("filters personas by name when the user types in the search box", async () => {
    renderPage();
    const input = (await screen.findAllByPlaceholderText(
      "ai.persona_search_placeholder",
    ))[0];
    fireEvent.change(input, { target: { value: "Lynch" } });
    // Lynch's display name should still be present after filter.
    expect(await screen.findByText("Peter Lynch")).toBeInTheDocument();
    // Buffett shouldn't render any more — search excludes other groups.
    expect(screen.queryByText("Warren Buffett")).not.toBeInTheDocument();
  });

  it("shows the empty-state message when no persona matches the search", async () => {
    renderPage();
    const input = (await screen.findAllByPlaceholderText(
      "ai.persona_search_placeholder",
    ))[0];
    fireEvent.change(input, { target: { value: "zzz_no_match" } });
    expect(await screen.findByText("ai.persona_search_empty")).toBeInTheDocument();
  });

  it("expands the group containing the active agent by default", async () => {
    // Default selectedAgent is the first agent in the array — market_analyst
    // (functional group). That group should be open on first render so the
    // user sees the active selection.
    renderPage();
    expect(await screen.findByText("Market Analyst")).toBeInTheDocument();
  });

  it("collapses non-active groups by default", async () => {
    renderPage();
    // Wait for personas to load.
    await screen.findByText("Market Analyst");
    // Buffett (value group) is in a non-active group → its card
    // should NOT render until the group is expanded.
    expect(screen.queryByText("Warren Buffett")).not.toBeInTheDocument();
  });

  it("toggles a group open when its header button is clicked", async () => {
    renderPage();
    await screen.findByText("Market Analyst");
    // Find the value group's header (the toggle button text comes from
    // i18n key `personas.groups.value.title` which the mock returns as
    // the key string itself).
    const valueHeader = screen.getByRole("button", {
      name: /personas\.groups\.value\.title/,
    });
    fireEvent.click(valueHeader);
    // Buffett's card now renders.
    expect(await screen.findByText("Warren Buffett")).toBeInTheDocument();
  });

  it("renders the inline Tools toggle (analyst+ role) instead of burying it in the dropdown", async () => {
    renderPage();
    // The chip is hidden on <sm but visible on sm+. Confirm the
    // button mounts in the DOM either way.
    const toggle = await screen.findByRole("button", {
      name: "ai.tools_toggle_aria",
    });
    expect(toggle).toBeInTheDocument();
    // Default state: tools off → label is `ai.tools_off`.
    expect(within(toggle).getByText("ai.tools_off")).toBeInTheDocument();
  });

  it("flips the inline Tools chip to ON when clicked", async () => {
    renderPage();
    const toggle = await screen.findByRole("button", {
      name: "ai.tools_toggle_aria",
    });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(within(toggle).getByText("ai.tools_on")).toBeInTheDocument();
  });
});
