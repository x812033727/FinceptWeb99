import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { PageHeader } from "./PageHeader";

describe("PageHeader", () => {
  it("renders the title as a single <h1>", () => {
    render(<PageHeader title="Portfolio" />);
    const h1 = screen.getByRole("heading", { level: 1 });
    expect(h1).toHaveTextContent("Portfolio");
  });

  it("applies the id to the <h1> so a region can aria-labelledby it", () => {
    render(<PageHeader title="Market" id="market-title" />);
    expect(screen.getByRole("heading", { level: 1 }).id).toBe("market-title");
  });

  it("renders the breadcrumb slot above the title", () => {
    render(<PageHeader title="2330 TSMC" breadcrumb={<span>TW · Stocks</span>} />);
    expect(screen.getByText("TW · Stocks")).toBeInTheDocument();
  });

  it("renders description and actions when given", () => {
    render(
      <PageHeader
        title="Screener"
        description="Filter the universe"
        actions={<button>Run</button>}
      />,
    );
    expect(screen.getByText("Filter the universe")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).toBeInTheDocument();
  });

  it("omits breadcrumb / description / actions when not provided", () => {
    const { container } = render(<PageHeader title="Bare" />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Bare");
    expect(container.querySelectorAll("button")).toHaveLength(0);
  });
});
