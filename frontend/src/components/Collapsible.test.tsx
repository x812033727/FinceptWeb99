import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { renderHook, act, render, screen, fireEvent } from "@testing-library/react";
import { CollapsibleHeader } from "./Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe("useCollapsible", () => {
  it("opens by default", () => {
    const { result } = renderHook(() => useCollapsible("test.a"));
    expect(result.current.open).toBe(true);
  });

  it("respects defaultOpen=false", () => {
    const { result } = renderHook(() => useCollapsible("test.b", false));
    expect(result.current.open).toBe(false);
  });

  it("toggles state", () => {
    const { result } = renderHook(() => useCollapsible("test.c"));
    act(() => result.current.toggle());
    expect(result.current.open).toBe(false);
    act(() => result.current.toggle());
    expect(result.current.open).toBe(true);
  });

  it("persists state to localStorage", () => {
    const { result } = renderHook(() => useCollapsible("test.d"));
    act(() => result.current.toggle());
    expect(localStorage.getItem("test.d")).toBe("0");
    act(() => result.current.toggle());
    expect(localStorage.getItem("test.d")).toBe("1");
  });

  it("rehydrates from localStorage", () => {
    localStorage.setItem("test.e", "0");
    const { result } = renderHook(() => useCollapsible("test.e"));
    expect(result.current.open).toBe(false);
  });

  it("falls back to default when localStorage value is malformed", () => {
    localStorage.setItem("test.f", "garbage");
    const { result } = renderHook(() => useCollapsible("test.f"));
    expect(result.current.open).toBe(true);
  });
});

describe("CollapsibleHeader", () => {
  it("renders title and chevron", () => {
    render(
      <CollapsibleHeader
        open={true}
        toggle={() => {}}
        title="My Card"
      />,
    );
    expect(screen.getByText("My Card")).toBeInTheDocument();
    expect(screen.getByText("▼")).toBeInTheDocument();
  });

  it("flips chevron when collapsed", () => {
    render(
      <CollapsibleHeader
        open={false}
        toggle={() => {}}
        title="My Card"
      />,
    );
    expect(screen.getByText("▶")).toBeInTheDocument();
  });

  it("invokes toggle on click", () => {
    let toggled = 0;
    render(
      <CollapsibleHeader
        open={true}
        toggle={() => { toggled += 1; }}
        title="My Card"
      />,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(toggled).toBe(1);
  });

  it("renders subtitle and headerRight", () => {
    render(
      <CollapsibleHeader
        open={true}
        toggle={() => {}}
        title="My Card"
        subtitle="extra context"
        headerRight={<span>BADGE</span>}
      />,
    );
    expect(screen.getByText("extra context")).toBeInTheDocument();
    expect(screen.getByText("BADGE")).toBeInTheDocument();
  });
});
