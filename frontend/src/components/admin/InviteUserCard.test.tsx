import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";
import InviteUserCard from "./InviteUserCard";

vi.mock("@/lib/api", () => ({
  default: { post: vi.fn() },
  errorDetail: (error: unknown) => error instanceof Error ? error.message : String(error),
}));

const post = vi.mocked(api.post);

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <InviteUserCard />
    </QueryClientProvider>,
  );
}

describe("InviteUserCard", () => {
  beforeEach(() => vi.clearAllMocks());

  it("creates an email-bound invitation and exposes its one-time activation link", async () => {
    post.mockResolvedValue({
      data: {
        id: "invite-1",
        email: "analyst@example.com",
        role: "analyst",
        expires_at: "2026-07-17T00:00:00Z",
        token: "raw-token",
      },
    });
    renderCard();

    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "analyst@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create invitation" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/admin/invitations", {
      email: "analyst@example.com",
      role: "analyst",
      expires_hours: 48,
    }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Invitation created for analyst@example.com",
    );
    expect(screen.getByLabelText("Activation link")).toHaveValue(
      "http://localhost:3000/accept-invite?token=raw-token&email=analyst%40example.com",
    );
    expect(screen.getByText(/raw token is not stored/i)).toBeInTheDocument();
  });

  it("renders the API error without exposing a link", async () => {
    post.mockRejectedValue(new Error("Email already has an account"));
    renderCard();
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "existing@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create invitation" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Email already has an account");
    expect(screen.queryByLabelText("Activation link")).not.toBeInTheDocument();
  });
});
