import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";
import NotificationChannels from "./NotificationChannels";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("@/lib/api", () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}));

const channels = [
  {
    kind: "email", enabled: true, verified: true, configured: true,
    destination_hint: "al***@example.com", event_kinds: ["price_alert", "strategy_health"],
    daily_digest: false, failed_count: 0, last_success_at: null,
  },
  {
    kind: "line", enabled: false, verified: false, configured: true,
    destination_hint: null, event_kinds: ["price_alert", "strategy_health"],
    daily_digest: false, failed_count: 0, last_success_at: null,
  },
];

function renderChannels() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <NotificationChannels />
    </QueryClientProvider>,
  );
}

describe("NotificationChannels", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.get).mockResolvedValue({ data: channels });
    vi.mocked(api.put).mockResolvedValue({ data: {} });
    vi.mocked(api.post).mockResolvedValue({ data: {} });
    vi.mocked(api.delete).mockResolvedValue({ data: {} });
  });

  it("renders provider readiness and alert-type filters", async () => {
    renderChannels();
    expect(await screen.findByText("settings.notifications.email")).toBeInTheDocument();
    expect(screen.getByText("al***@example.com")).toBeInTheDocument();
    expect(screen.getByText("settings.notifications.line")).toBeInTheDocument();
    expect(screen.getAllByText("settings.notifications.event_price_alert")).toHaveLength(2);
  });

  it("updates a channel without dropping its event filters", async () => {
    renderChannels();
    await screen.findByText("settings.notifications.email");
    const disable = screen.getByRole("button", { name: "settings.notifications.disable" });
    fireEvent.click(disable);
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(
      "/notifications/channels/email",
      { enabled: false, event_kinds: ["price_alert", "strategy_health"], daily_digest: false },
    ));
  });

  it("starts the one-time LINE binding flow and shows only the user message", async () => {
    vi.mocked(api.post).mockResolvedValueOnce({
      data: {
        token: "one-time-token",
        expires_at: "2026-07-15T12:15:00Z",
        instruction: "server instruction",
      },
    });
    renderChannels();
    const connect = await screen.findByRole("button", {
      name: "settings.notifications.connect_line",
    });
    fireEvent.click(connect);
    expect(await screen.findByText("FINCEPT one-time-token")).toBeInTheDocument();
    expect(screen.queryByText("server instruction")).not.toBeInTheDocument();
  });

  it("sends a test notification for a verified channel", async () => {
    renderChannels();
    const send = await screen.findByRole("button", { name: "settings.notifications.send_test" });
    fireEvent.click(send);
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      "/notifications/channels/email/test",
    ));
  });
});
