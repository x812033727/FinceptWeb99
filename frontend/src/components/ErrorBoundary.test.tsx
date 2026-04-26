/**
 * Tests for the ErrorBoundary component.
 *
 * Throwing a child renders the fallback. The fallback contains:
 *  - the i18n'd "Something went wrong" header
 *  - the error message (or the unexpected_error fallback)
 *  - a "Try again" button that resets state + remounts children
 *  - a "Reload page" button that calls window.location.reload
 *
 * React logs caught errors to console.error during boundary catch;
 * silenced per-suite to keep the test output readable.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useState } from "react";
import { ErrorBoundary } from "./ErrorBoundary";

const Boom = ({ message }: { message: string }) => {
  throw new Error(message);
};

const StatefulChild = ({ throwOnFirstRender }: { throwOnFirstRender: boolean }) => {
  const [thrown] = useState(throwOnFirstRender);
  if (thrown) throw new Error("first render bomb");
  return <div data-testid="happy">recovered</div>;
};

describe("ErrorBoundary", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders children when no error is thrown", () => {
    render(
      <ErrorBoundary>
        <span data-testid="ok">all good</span>
      </ErrorBoundary>
    );
    expect(screen.getByTestId("ok")).toBeInTheDocument();
  });

  it("renders the default fallback with the thrown message + both action buttons", () => {
    render(
      <ErrorBoundary>
        <Boom message="kaboom" />
      </ErrorBoundary>
    );
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByText("kaboom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload page" })).toBeInTheDocument();
  });

  it("falls back to the unexpected-error copy when error.message is empty", () => {
    const Empty = () => {
      throw new Error();
    };
    render(
      <ErrorBoundary>
        <Empty />
      </ErrorBoundary>
    );
    expect(screen.getByText("An unexpected error occurred.")).toBeInTheDocument();
  });

  it("renders a custom fallback when one is provided (overriding the default UI)", () => {
    render(
      <ErrorBoundary fallback={<span data-testid="custom">custom fallback</span>}>
        <Boom message="x" />
      </ErrorBoundary>
    );
    expect(screen.getByTestId("custom")).toBeInTheDocument();
    expect(screen.queryByText("Something went wrong")).not.toBeInTheDocument();
  });

  it("Try again resets error state — on remount the child can render normally", () => {
    // First render throws; rerender with a non-throwing child first
    // (the boundary stays in error state holding the old subtree),
    // then click Try again to reset and remount with the new
    // non-throwing child.
    const { rerender } = render(
      <ErrorBoundary>
        <StatefulChild throwOnFirstRender={true} />
      </ErrorBoundary>
    );
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();

    rerender(
      <ErrorBoundary>
        <StatefulChild throwOnFirstRender={false} />
      </ErrorBoundary>
    );
    // Boundary still shows fallback because state.hasError is true
    // even though the children prop has changed.
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(screen.getByTestId("happy")).toHaveTextContent("recovered");
    expect(screen.queryByText("Something went wrong")).not.toBeInTheDocument();
  });

  it("Reload page calls window.location.reload", () => {
    const reload = vi.fn();
    // jsdom's location.reload is non-configurable in some versions —
    // patch the method via Object.defineProperty so we can spy on it.
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, reload },
    });

    render(
      <ErrorBoundary>
        <Boom message="x" />
      </ErrorBoundary>
    );
    fireEvent.click(screen.getByRole("button", { name: "Reload page" }));
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
