import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import type { Turn } from "@/types/discussion";
import { TurnBubble } from "./TurnBubble";

const personaName = (id: string) =>
  ({ buffett: "Warren Buffett", market_analyst: "Market Analyst" }[id] ?? id);

function makeTurn(overrides: Partial<Turn> = {}): Turn {
  return {
    id: 1,
    round: 1,
    turn_index: 0,
    persona_id: "buffett",
    stance: "agree",
    content: "RSI 67 已逼近超買",
    created_at: "2026-05-03T00:00:00Z",
    ...overrides,
  };
}

describe("TurnBubble", () => {
  it("renders the persona name + body content", () => {
    render(<TurnBubble turn={makeTurn()} personaName={personaName} />);
    expect(screen.getByText("Warren Buffett")).toBeInTheDocument();
    expect(screen.getByText("RSI 67 已逼近超買")).toBeInTheDocument();
  });

  it("shows the per-persona accent colour on the left border", () => {
    /* jsdom normalises hsl() to rgb() on style read-back, so we just
       confirm an inline border-left-color is set (rather than the
       literal HSL string). */
    const { container } = render(
      <TurnBubble turn={makeTurn()} personaName={personaName} personaInitial="BU" />,
    );
    const article = container.querySelector("article") as HTMLElement;
    const style = article.getAttribute("style") ?? "";
    expect(style).toMatch(/border-left-color:\s*[a-z]/i);
  });

  it("renders an avatar with the explicit initial", () => {
    render(
      <TurnBubble
        turn={makeTurn()}
        personaName={personaName}
        personaInitial="BU"
      />,
    );
    expect(screen.getByText("BU")).toBeInTheDocument();
  });

  it("uses an icon-only stance badge in the corner instead of an inline chip", () => {
    /* PR-C: stance moved out of the persona-name row into a small
       icon-only square in the top-right. The badge carries an
       aria-label so screen readers still get the stance semantic. */
    render(<TurnBubble turn={makeTurn({ stance: "dissent" })} personaName={personaName} />);
    expect(screen.getByLabelText("Dissent")).toBeInTheDocument();
  });

  it("substitutes the agree-silent placeholder when an `agree` turn has empty content", () => {
    const { container } = render(
      <TurnBubble
        turn={makeTurn({ stance: "agree", content: "" })}
        personaName={personaName}
      />,
    );
    // The body wrapper shouldn't be empty.
    const body = container.querySelector(".whitespace-pre-wrap");
    expect((body?.textContent ?? "").trim()).not.toBe("");
  });

  it("keeps an empty `dissent` turn body empty (placeholder only fires for agree)", () => {
    const { container } = render(
      <TurnBubble
        turn={makeTurn({ stance: "dissent", content: "" })}
        personaName={personaName}
      />,
    );
    const body = container.querySelector(".whitespace-pre-wrap");
    expect((body?.textContent ?? "").trim()).toBe("");
  });

  it("applies the streaming ring + cursor when `streaming` is true", () => {
    const { container } = render(
      <TurnBubble turn={makeTurn()} personaName={personaName} streaming />,
    );
    const article = container.querySelector("article") as HTMLElement;
    expect(article.className).toContain("ring-2");
    expect(container.querySelector(".animate-pulse")).not.toBeNull();
  });

  it("renders the abstain stance badge for no-response placeholder turns", () => {
    render(
      <TurnBubble
        turn={makeTurn({
          stance: "abstain",
          content: "（此輪因 LLM 300s 內未回覆而中止）",
        })}
        personaName={personaName}
      />,
    );
    expect(screen.getByLabelText("No response")).toBeInTheDocument();
  });

  // ── B4: interjection badges ─────────────────────────────────────

  it("badges the owner's interjected question (user_input stance)", () => {
    render(
      <TurnBubble
        turn={makeTurn({
          persona_id: "_user",
          stance: "user_input",
          injected_by_user: true,
          content: "為什麼看好 2330?",
        })}
        personaName={(id) => (id === "_user" ? "You" : id)}
      />,
    );
    expect(screen.getByText("User question")).toBeInTheDocument();
  });

  it("badges a persona turn answering an interjection", () => {
    render(
      <TurnBubble
        turn={makeTurn({ stance: "supplement", injected_by_user: true })}
        personaName={personaName}
      />,
    );
    expect(screen.getByText("Interjection reply")).toBeInTheDocument();
  });

  it("shows no interjection badge on ordinary persona turns", () => {
    render(
      <TurnBubble
        turn={makeTurn({ stance: "supplement" })}
        personaName={personaName}
      />,
    );
    expect(screen.queryByText("Interjection reply")).not.toBeInTheDocument();
    expect(screen.queryByText("User question")).not.toBeInTheDocument();
  });
});
