/**
 * Tests for `DiscussionToolbar` (PR-E layout redesign).
 *
 * The toolbar replaces the four vertically-stacked sidebar cards
 * with a horizontal row of one CTA + three Popover triggers. These
 * tests verify (a) the four expected affordances render, (b) the
 * AutoRun ON badge surfaces from the shared TanStack Query cache
 * when the user has opted in, and (c) clicking 「+ 新討論」 fires
 * the parent's reset handler.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockFetchAutoRunConfig = vi.fn();

vi.mock("./_helpers", async () => {
  const actual = await vi.importActual<typeof import("./_helpers")>(
    "./_helpers",
  );
  return {
    ...actual,
    fetchAutoRunConfig: (...args: unknown[]) =>
      mockFetchAutoRunConfig(...args),
  };
});

import { DiscussionToolbar } from "./DiscussionToolbar";

beforeEach(() => {
  mockFetchAutoRunConfig.mockReset();
});

function renderToolbar({
  autoRunEnabled = false,
  onNewDiscussion = vi.fn(),
  withAgents = false,
}: {
  autoRunEnabled?: boolean;
  onNewDiscussion?: () => void;
  withAgents?: boolean;
} = {}) {
  mockFetchAutoRunConfig.mockResolvedValue({
    enabled: autoRunEnabled,
    persona_ids: [],
    topic: "",
    rules: "",
    market: "TW",
    send_email: false,
    strategy_run_counts: { general: 0, chip_momentum: 0, quality_growth: 0, breakout: 0, oversold_reversal: 0 },
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DiscussionToolbar
        agents={withAgents ? [
          { id: "buffett", name: "Buffett", description: "", default_provider: "openai" },
          { id: "lynch", name: "Lynch", description: "", default_provider: "openai" },
        ] : []}
        personaName={(id) => id}
        onNewDiscussion={onNewDiscussion}
        topic=""
        rules=""
        market="TW"
        personaIds={[]}
      />
    </QueryClientProvider>,
  );
}

describe("DiscussionToolbar", () => {
  it("renders the four affordances: + 新, Auto-run, Strategy, Sweep", () => {
    renderToolbar();
    // i18n in test setup → en strings.
    expect(screen.getByRole("button", { name: /New$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Auto-run/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Strategy/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sweep/i })).toBeInTheDocument();
  });

  it("hides the ON badge when auto-run is disabled", () => {
    renderToolbar({ autoRunEnabled: false });
    expect(screen.queryByText("ON")).not.toBeInTheDocument();
  });

  it("renders the ON badge when auto-run config has enabled=true", async () => {
    renderToolbar({ autoRunEnabled: true });
    // Wait for the TanStack Query mock to resolve and badge to mount.
    expect(await screen.findByText("ON")).toBeInTheDocument();
  });

  it("fires the new-discussion handler when 「+ 新」 is clicked", () => {
    const onNewDiscussion = vi.fn();
    renderToolbar({ onNewDiscussion });
    fireEvent.click(screen.getByRole("button", { name: /New$/i }));
    expect(onNewDiscussion).toHaveBeenCalledTimes(1);
  });

  it("uses role=toolbar for assistive tech grouping", () => {
    renderToolbar();
    expect(screen.getByRole("toolbar")).toBeInTheDocument();
  });

  it("opens auto-run as an interactive dialog", async () => {
    renderToolbar({ withAgents: true });
    fireEvent.click(screen.getByRole("button", { name: /Auto-run/i }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();

    const buffett = await screen.findByRole("checkbox", { name: "buffett" });
    fireEvent.click(buffett);
    expect(buffett).toBeChecked();

    const general = screen.getByRole("checkbox", { name: "啟用綜合選股" });
    fireEvent.click(general);
    expect(general).toBeChecked();

    expect(screen.getByRole("button", { name: /Save settings/i })).toBeEnabled();
  });
});
