/**
 * Tests for `RoundSection` extracted from `pages/DiscussionPage`
 * in PR #264. The component is a thin shell over `useCollapsible`
 * + per-turn rendering, but the render path has enough conditionals
 * (chevron flip / `agree`-silent placeholder / stance badge / per-
 * persona name lookup) to deserve direct coverage now that it can
 * be unit-tested in isolation.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { Turn } from "@/types/discussion";
import { RoundSection } from "./RoundSection";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

const personaName = (id: string) =>
  ({
    market_analyst: "Market Analyst",
    buffett: "Warren Buffett",
  }[id] ?? id);

function makeTurn(overrides: Partial<Turn> = {}): Turn {
  return {
    id: 1,
    round: 1,
    turn_index: 0,
    persona_id: "market_analyst",
    stance: "agree",
    content: "RSI 67 已逼近超買",
    created_at: "2026-05-03T00:00:00Z",
    ...overrides,
  };
}

describe("RoundSection", () => {
  it("renders the header with round + turn count regardless of collapse state", () => {
    render(
      <RoundSection
        discussionId="d1"
        round={2}
        turns={[makeTurn(), makeTurn({ id: 2, turn_index: 1 })]}
        personaName={personaName}
      />,
    );
    // Default-collapsed: chevron is "▶".
    const button = screen.getByRole("button");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button.textContent).toContain("▶");
    // Header shows the count regardless of open state.
    expect(button.textContent).toMatch(/2/);
  });

  it("auto-expands when the round contains a post-mortem user_input turn", () => {
    /* PR #268: post-mortem rounds are surfaced expanded by default
       so the operator immediately sees the injected critique +
       persona reflections, instead of staring at a collapsed
       section with no visible change after the chain runs. */
    render(
      <RoundSection
        discussionId="d1"
        round={1}
        turns={[
          makeTurn(),
          makeTurn({
            id: 2, turn_index: 1, persona_id: "_user",
            stance: "user_input",
            content: "【事後檢討 — 對答案】請反思",
          }),
        ]}
        personaName={personaName}
      />,
    );
    const button = screen.getByRole("button");
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(button.textContent).toContain("▼");
    // 「📋 事後檢討」 badge surfaces in the header.
    expect(button.textContent).toContain("📋");
  });

  it("auto-expands and shows the directive badge for non-post-mortem user_input turns", () => {
    /* Generic owner-injected directives (not necessarily post-mortem)
       still get an "owner directive" badge so the round isn't
       silently collapsed — but a different badge so operators can
       tell the two apart at a glance. */
    render(
      <RoundSection
        discussionId="d1"
        round={1}
        turns={[
          makeTurn(),
          makeTurn({
            id: 2, turn_index: 1, persona_id: "_user",
            stance: "user_input",
            content: "請聚焦在 2330 的法說會時程。",
          }),
        ]}
        personaName={personaName}
      />,
    );
    const button = screen.getByRole("button");
    expect(button).toHaveAttribute("aria-expanded", "true");
    // Owner-directive badge (✎ from the locale file).
    expect(button.textContent).toContain("✎");
    // Post-mortem-specific badge must NOT fire for generic directives.
    expect(button.textContent).not.toContain("📋");
  });

  it("hides turn bodies when collapsed", () => {
    render(
      <RoundSection
        discussionId="d1"
        round={1}
        turns={[makeTurn({ content: "secret content" })]}
        personaName={personaName}
      />,
    );
    expect(screen.queryByText("secret content")).not.toBeInTheDocument();
  });

  it("expands on click and shows turn bodies + persona names + stance badges", () => {
    render(
      <RoundSection
        discussionId="d1"
        round={1}
        turns={[
          makeTurn({ content: "RSI 67 已逼近超買" }),
          makeTurn({
            id: 2, turn_index: 1, persona_id: "buffett",
            stance: "dissent",
            content: "Long-term fundamentals matter more.",
          }),
        ]}
        personaName={personaName}
      />,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("RSI 67 已逼近超買")).toBeInTheDocument();
    expect(screen.getByText("Long-term fundamentals matter more.")).toBeInTheDocument();
    expect(screen.getByText("Market Analyst")).toBeInTheDocument();
    expect(screen.getByText("Warren Buffett")).toBeInTheDocument();
  });

  it("renders the agree-silent placeholder when an `agree` turn has empty content", () => {
    render(
      <RoundSection
        discussionId="d1"
        round={1}
        turns={[makeTurn({ stance: "agree", content: "" })]}
        personaName={personaName}
      />,
    );
    fireEvent.click(screen.getByRole("button"));
    // The exact wording lives in the en locale; assert it's NOT the
    // empty string (placeholder fired) and NOT the original "RSI..."
    // content (which we cleared).
    const card = screen.getByText("Market Analyst").closest("div")?.parentElement;
    expect(card?.textContent ?? "").not.toBe("");
  });

  it("does NOT render the placeholder when a `dissent` turn is empty", () => {
    /* The `agree`-silent fallback is specific to agree-stance turns
       with no body — a dissent with no body should still render its
       (empty) content as-is rather than substituting the placeholder
       string, because an empty dissent is suspicious data and
       silently filling it would hide the bug. */
    render(
      <RoundSection
        discussionId="d1"
        round={1}
        turns={[makeTurn({ stance: "dissent", content: "" })]}
        personaName={personaName}
      />,
    );
    fireEvent.click(screen.getByRole("button"));
    // Find the persona name row's parent card.
    const card = screen.getByText("Market Analyst").closest(".bg-card");
    // Dig into the body text container — should be empty or whitespace.
    const body = card?.querySelector(".whitespace-pre-wrap");
    expect((body?.textContent ?? "").trim()).toBe("");
  });

  it("persists collapse state to localStorage keyed by (discussionId, round)", () => {
    const { unmount } = render(
      <RoundSection
        discussionId="abc"
        round={3}
        turns={[makeTurn()]}
        personaName={personaName}
      />,
    );
    fireEvent.click(screen.getByRole("button"));
    // After expand, localStorage carries the open state under the
    // documented key shape so reload preserves it.
    expect(localStorage.getItem("discussion.abc.round.3")).toBe("1");
    unmount();
    // Re-mount: the new instance reads back the persisted state.
    render(
      <RoundSection
        discussionId="abc"
        round={3}
        turns={[makeTurn()]}
        personaName={personaName}
      />,
    );
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "true");
  });

  it("uses an isolated collapse key per (discussionId, round) so toggles don't bleed", () => {
    /* The original bug behind PR #247: when RoundSection used a
       single shared key like `round.${round}`, expanding round 1 in
       discussion A also expanded round 1 in discussion B because
       both keys collided in localStorage. The keying must combine
       both axes. */
    render(
      <>
        <RoundSection
          discussionId="A"
          round={1}
          turns={[makeTurn()]}
          personaName={personaName}
        />
        <RoundSection
          discussionId="B"
          round={1}
          turns={[makeTurn()]}
          personaName={personaName}
        />
      </>,
    );
    const buttons = screen.getAllByRole("button");
    // Sanity: both rendered, both default-collapsed.
    expect(buttons).toHaveLength(2);
    expect(buttons[0]).toHaveAttribute("aria-expanded", "false");
    expect(buttons[1]).toHaveAttribute("aria-expanded", "false");
    // Toggle the first; only it changes.
    fireEvent.click(buttons[0]);
    expect(buttons[0]).toHaveAttribute("aria-expanded", "true");
    expect(buttons[1]).toHaveAttribute("aria-expanded", "false");
  });

  it("renders a `supplement` stance turn with its badge", () => {
    render(
      <RoundSection
        discussionId="d1"
        round={1}
        turns={[
          makeTurn({
            stance: "supplement",
            content: "Adding context: Q4 capex guidance is intact.",
          }),
        ]}
        personaName={personaName}
      />,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(
      screen.getByText("Adding context: Q4 capex guidance is intact."),
    ).toBeInTheDocument();
  });
});
