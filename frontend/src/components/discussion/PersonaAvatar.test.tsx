import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { PersonaAvatar } from "./PersonaAvatar";

describe("PersonaAvatar", () => {
  it("renders the explicit initial when provided", () => {
    render(<PersonaAvatar personaId="buffett" name="Warren Buffett" initial="BU" />);
    expect(screen.getByText("BU")).toBeInTheDocument();
  });

  it("derives the initial from the localised name when no explicit initial is given", () => {
    render(<PersonaAvatar personaId="buffett" name="巴菲特 Warren Buffett" />);
    expect(screen.getByText("巴")).toBeInTheDocument();
  });

  it("applies an inline colour style so the same id always renders the same hue", () => {
    /* Note: jsdom normalises `hsl(...)` inline styles to `rgb()` on
       read-back, so we can't assert the literal HSL string. We do
       verify (a) some inline colour is present, and (b) the same id
       produces an identical inline-style string between renders so
       the hue is deterministic. */
    const { container: a } = render(
      <PersonaAvatar personaId="lynch" name="Peter Lynch" initial="LY" />,
    );
    const { container: b } = render(
      <PersonaAvatar personaId="lynch" name="Peter Lynch" initial="LY" />,
    );
    const styleA = (a.firstChild as HTMLElement).getAttribute("style") ?? "";
    const styleB = (b.firstChild as HTMLElement).getAttribute("style") ?? "";
    expect(styleA).toContain("color:");
    expect(styleA).toContain("background-color:");
    expect(styleA).toBe(styleB);
  });

  it("forwards aria-label + title to the localised name for a11y / hover tooltip", () => {
    render(<PersonaAvatar personaId="buffett" name="Warren Buffett" initial="BU" />);
    const el = screen.getByLabelText("Warren Buffett");
    expect(el.getAttribute("title")).toBe("Warren Buffett");
  });

  it("respects the size prop — sm uses 6×6, md uses 7×7", () => {
    const sm = render(
      <PersonaAvatar personaId="x" name="X" initial="X" size="sm" />,
    );
    expect((sm.container.firstChild as HTMLElement).className).toContain("h-6");

    const md = render(
      <PersonaAvatar personaId="x" name="X" initial="X" size="md" />,
    );
    expect((md.container.firstChild as HTMLElement).className).toContain("h-7");
  });
});
