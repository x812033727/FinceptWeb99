/**
 * Tests for FinmindKeysCard.
 *
 * The headline invariant: plaintext key is shown ONCE in the amber
 * banner after issuance, and is NOT included in the subsequent
 * listing query response (the proxy strips it server-side, but the
 * frontend should also handle the case gracefully).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";

import FinmindKeysCard from "./FinmindKeysCard";

vi.mock("@/lib/api", () => ({
  default: {
    get: vi.fn(),
    patch: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
  errorDetail: (err: unknown) =>
    err instanceof Error ? err.message : String(err),
}));

const mockedApi = api as unknown as {
  get: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
  delete: ReturnType<typeof vi.fn>;
};


function renderCard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <FinmindKeysCard />
    </QueryClientProvider>,
  );
}


beforeEach(() => {
  localStorage.setItem("admin-finmind-keys", "1");
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});


describe("FinmindKeysCard — empty state", () => {
  it("renders the empty-state hint when no keys exist", async () => {
    mockedApi.get.mockResolvedValue({ data: [] });
    renderCard();
    expect(
      await screen.findByText(/No API keys yet/i),
    ).toBeInTheDocument();
  });
});


describe("FinmindKeysCard — issuance flow", () => {
  beforeEach(() => {
    mockedApi.get.mockResolvedValue({ data: [] });
  });

  it("disables the Issue button when owner email is empty", async () => {
    renderCard();
    const button = await screen.findByRole("button", { name: /Issue key/i });
    expect(button).toBeDisabled();
  });

  it("POSTs to /keys with owner_email + name when submitted", async () => {
    mockedApi.post.mockResolvedValue({
      data: {
        record_id: 1,
        plaintext: "fck_live_test_dontuseforreal",
        prefix: "fck_live_t",
        owner_email: "customer@example.com",
      },
    });

    renderCard();
    const emailInput = await screen.findByPlaceholderText(/customer@example.com/i);
    fireEvent.change(emailInput, {
      target: { value: "customer@example.com" },
    });
    const nameInput = screen.getByPlaceholderText(/prod-backtest/i);
    fireEvent.change(nameInput, { target: { value: "prod" } });

    const button = screen.getByRole("button", { name: /Issue key/i });
    fireEvent.click(button);

    await waitFor(() => {
      expect(mockedApi.post).toHaveBeenCalledWith(
        "/admin/finmind/keys",
        { owner_email: "customer@example.com", name: "prod" },
      );
    });
  });

  it("displays the plaintext in the amber banner after issuance", async () => {
    mockedApi.post.mockResolvedValue({
      data: {
        record_id: 1,
        plaintext: "fck_live_test_dontuseforreal_xx12",
        prefix: "fck_live_t",
        owner_email: "customer@example.com",
      },
    });

    renderCard();
    const emailInput = await screen.findByPlaceholderText(/customer@example.com/i);
    fireEvent.change(emailInput, {
      target: { value: "customer@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Issue key/i }));

    expect(
      await screen.findByText(/Copy this key now/i),
    ).toBeInTheDocument();
    // Plaintext rendered in the banner so operator can copy it.
    expect(
      screen.getByText("fck_live_test_dontuseforreal_xx12"),
    ).toBeInTheDocument();
  });
});


describe("FinmindKeysCard — listing + revoke", () => {
  const SAMPLE_KEYS = [
    {
      id: 42,
      prefix: "fck_live_a",
      owner_email: "alice@example.com",
      name: "alice-prod",
      enabled: true,
      expires_at: null,
      last_used_at: "2026-05-01T00:00:00+00:00",
      created_at: "2026-04-01T00:00:00+00:00",
      plan_code: null,
      subscription_id: null,
    },
    {
      id: 43,
      prefix: "fck_live_b",
      owner_email: "bob@example.com",
      name: null,
      enabled: false,
      expires_at: null,
      last_used_at: null,
      created_at: "2026-04-15T00:00:00+00:00",
      plan_code: null,
      subscription_id: null,
    },
  ];

  // The card now fetches BOTH /keys AND /plans in parallel; each
  // useQuery renders independently. A mockResolvedValue that returned
  // SAMPLE_KEYS for every URL would feed key-shaped objects into the
  // plans render path, where `quota_daily_calls.toLocaleString()`
  // crashes on undefined. Route by URL so each query gets its right
  // shape.
  function routeByUrl(url: string): { data: unknown } {
    if (url.includes("/plans")) return { data: [] };
    if (url.includes("/keys")) return { data: SAMPLE_KEYS };
    return { data: [] };
  }

  it("renders the keys table with prefix + owner + status", async () => {
    mockedApi.get.mockImplementation(async (url: string) => routeByUrl(url));
    renderCard();

    expect(await screen.findByText("alice@example.com")).toBeInTheDocument();
    expect(screen.getByText("bob@example.com")).toBeInTheDocument();
    // Enabled badge for Alice's key
    expect(screen.getByText("enabled")).toBeInTheDocument();
    // Revoked badge for Bob's
    expect(screen.getByText("revoked")).toBeInTheDocument();
  });

  it("calls DELETE when Revoke is confirmed", async () => {
    mockedApi.get.mockImplementation(async (url: string) => routeByUrl(url));
    mockedApi.delete.mockResolvedValue({});
    // Auto-confirm the window.confirm prompt so the test doesn't hang.
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderCard();
    await screen.findByText("alice@example.com");

    const revokeBtn = screen.getByRole("button", { name: /Revoke/i });
    fireEvent.click(revokeBtn);

    await waitFor(() => {
      expect(mockedApi.delete).toHaveBeenCalledWith(
        "/admin/finmind/keys/42",
      );
    });
  });

  it("does NOT call DELETE if the operator cancels the confirm dialog", async () => {
    mockedApi.get.mockImplementation(async (url: string) => routeByUrl(url));
    mockedApi.delete.mockResolvedValue({});
    vi.spyOn(window, "confirm").mockReturnValue(false);

    renderCard();
    await screen.findByText("alice@example.com");
    const revokeBtn = screen.getByRole("button", { name: /Revoke/i });
    fireEvent.click(revokeBtn);

    // Wait long enough that any naive fire-and-forget delete would
    // have hit the mock; assert it didn't.
    await new Promise((r) => setTimeout(r, 50));
    expect(mockedApi.delete).not.toHaveBeenCalled();
  });
});
