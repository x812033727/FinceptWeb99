import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Inbox } from "lucide-react";
import { EmptyState } from "./EmptyState";
import { PageHeader } from "./PageHeader";
import { StatCard } from "./StatCard";

describe("EmptyState", () => {
  it("renders title, description, action and icon", () => {
    const { container } = render(
      <EmptyState
        icon={Inbox}
        title="No data"
        description="Try widening the date range."
        action={<button>Reload</button>}
      />
    );
    expect(screen.getByText("No data")).toBeTruthy();
    expect(screen.getByText("Try widening the date range.")).toBeTruthy();
    expect(screen.getByText("Reload")).toBeTruthy();
    expect(container.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
  });

  it("renders without optional parts", () => {
    render(<EmptyState title="Empty" />);
    expect(screen.getByText("Empty")).toBeTruthy();
  });
});

describe("PageHeader", () => {
  it("renders h1 title with description and actions", () => {
    render(
      <PageHeader title="Portfolio" description="All accounts" actions={<button>New</button>} />
    );
    const h1 = screen.getByRole("heading", { level: 1 });
    expect(h1.textContent).toBe("Portfolio");
    expect(screen.getByText("All accounts")).toBeTruthy();
    expect(screen.getByText("New")).toBeTruthy();
  });
});

describe("StatCard", () => {
  it("renders label, value, delta and hint", () => {
    render(
      <StatCard label="TAIEX" value="30,313" delta={<span>+2.8%</span>} hint="as of 13:30" />
    );
    expect(screen.getByText("TAIEX")).toBeTruthy();
    expect(screen.getByText("30,313")).toBeTruthy();
    expect(screen.getByText("+2.8%")).toBeTruthy();
    expect(screen.getByText("as of 13:30")).toBeTruthy();
  });
});
