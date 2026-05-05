/**
 * Tests for useMediaQuery — covers initial-state read from matchMedia
 * and listener wire-up + tear-down.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useMediaQuery } from "./useMediaQuery";

interface MockMQL {
  matches: boolean;
  media: string;
  addEventListener: ReturnType<typeof vi.fn>;
  removeEventListener: ReturnType<typeof vi.fn>;
  trigger: (matches: boolean) => void;
}

let mockMQL: MockMQL;

beforeEach(() => {
  const handlers: Array<(e: MediaQueryListEvent) => void> = [];
  mockMQL = {
    matches: false,
    media: "",
    addEventListener: vi.fn((_evt: string, h: (e: MediaQueryListEvent) => void) => {
      handlers.push(h);
    }),
    removeEventListener: vi.fn((_evt: string, h: (e: MediaQueryListEvent) => void) => {
      const i = handlers.indexOf(h);
      if (i >= 0) handlers.splice(i, 1);
    }),
    trigger(matches: boolean) {
      this.matches = matches;
      handlers.forEach((h) => h({ matches } as MediaQueryListEvent));
    },
  };
  window.matchMedia = vi.fn().mockImplementation((q: string) => {
    mockMQL.media = q;
    return mockMQL;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useMediaQuery", () => {
  it("reads initial match state from matchMedia", () => {
    mockMQL.matches = true;
    const { result } = renderHook(() => useMediaQuery("(min-width: 768px)"));
    expect(result.current).toBe(true);
  });

  it("re-renders when the media query transitions", () => {
    mockMQL.matches = false;
    const { result } = renderHook(() => useMediaQuery("(min-width: 768px)"));
    expect(result.current).toBe(false);
    act(() => {
      mockMQL.trigger(true);
    });
    expect(result.current).toBe(true);
  });

  it("removes its listener on unmount", () => {
    const { unmount } = renderHook(() => useMediaQuery("(min-width: 768px)"));
    expect(mockMQL.addEventListener).toHaveBeenCalledTimes(1);
    unmount();
    expect(mockMQL.removeEventListener).toHaveBeenCalledTimes(1);
  });

  it("re-subscribes when the query string changes", () => {
    const { rerender } = renderHook(({ q }: { q: string }) => useMediaQuery(q), {
      initialProps: { q: "(min-width: 640px)" },
    });
    expect(mockMQL.addEventListener).toHaveBeenCalledTimes(1);
    rerender({ q: "(min-width: 1024px)" });
    expect(mockMQL.removeEventListener).toHaveBeenCalledTimes(1);
    expect(mockMQL.addEventListener).toHaveBeenCalledTimes(2);
  });
});
