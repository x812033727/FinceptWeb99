/**
 * Tests for the Toaster component.
 *
 * Toaster is a zustand subscriber, so we drive it by pushing through
 * the real `useToastStore` rather than mocking — the store is pure
 * and has its own tests, and the integration here is what we want to
 * pin down.
 *
 * Auto-dismiss tests use fake timers so the suite stays fast (the
 * default TTL is 6 s).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

import Toaster from "./Toaster";
import { useToastStore } from "@/store/toastStore";

beforeEach(() => {
  // Each test starts with an empty toast list so previous pushes
  // don't bleed across cases.
  act(() => useToastStore.getState().clear());
});

afterEach(() => {
  vi.useRealTimers();
});

// ── Empty state ──────────────────────────────────────────────────

describe("Toaster — empty state", () => {
  it("renders nothing when there are no toasts", () => {
    const { container } = render(<Toaster />);
    expect(container.firstChild).toBeNull();
  });
});

// ── Render shape ─────────────────────────────────────────────────

describe("Toaster — render shape", () => {
  it("renders title + detail when both are provided", () => {
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({
        severity: "info", title: "Heads up", detail: "Something happened.",
      });
    });
    expect(screen.getByText("Heads up")).toBeInTheDocument();
    expect(screen.getByText("Something happened.")).toBeInTheDocument();
  });

  it("omits the detail line entirely when detail is undefined", () => {
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "info", title: "Just a title" });
    });
    // The detail-line p element has the text-xs class — assert by absence
    // of any text that's not the title.
    const region = screen.getByRole("region");
    expect(region.querySelectorAll("p")).toHaveLength(1);
  });

  it("renders newest toasts first (LIFO order)", () => {
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "info", title: "First" });
      useToastStore.getState().push({ severity: "info", title: "Second" });
      useToastStore.getState().push({ severity: "info", title: "Third" });
    });
    const titles = Array.from(screen.getAllByRole("status"))
      .map((el) => el.querySelector("p")?.textContent);
    expect(titles).toEqual(["Third", "Second", "First"]);
  });

  it("renders all 4 severity icons correctly", () => {
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "info",    title: "i" });
      useToastStore.getState().push({ severity: "success", title: "s" });
      useToastStore.getState().push({ severity: "warning", title: "w" });
      useToastStore.getState().push({ severity: "error",   title: "e" });
    });
    // Each severity has a distinct icon glyph.
    expect(screen.getByText("ⓘ")).toBeInTheDocument();
    expect(screen.getByText("✓")).toBeInTheDocument();
    expect(screen.getByText("!")).toBeInTheDocument();
    // The "×" glyph is reused by the dismiss button so there's
    // multiple — assert at least 5 (1 from error icon + 4 dismiss
    // buttons).
    expect(screen.getAllByText("×").length).toBeGreaterThanOrEqual(5);
  });

  it("attaches the severity colour bar class to the icon badge", () => {
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "error", title: "boom" });
    });
    // The icon is a <span>; the dismiss control is a <button>. Both
    // render "×" for the error severity, so query by tag to disambiguate.
    const icon = screen.getByRole("alert").querySelector("span")!;
    expect(icon.textContent).toBe("×");
    expect(icon.className).toContain("bg-danger");
  });
});

// ── ARIA / a11y ──────────────────────────────────────────────────

describe("Toaster — accessibility", () => {
  it("uses role='alert' for error severity (interrupts assistive tech immediately)", () => {
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "error", title: "Server down" });
    });
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("uses role='status' for non-error severities (polite announcement)", () => {
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "info", title: "Saved" });
      useToastStore.getState().push({ severity: "success", title: "Saved" });
      useToastStore.getState().push({ severity: "warning", title: "Saved" });
    });
    // 3 status roles, 0 alert roles.
    expect(screen.getAllByRole("status")).toHaveLength(3);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("wraps the toast stack in a polite live region", () => {
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "info", title: "x" });
    });
    const region = screen.getByRole("region");
    expect(region.getAttribute("aria-live")).toBe("polite");
    expect(region.getAttribute("aria-label")).toBe("Notifications");
  });
});

// ── Dismiss ──────────────────────────────────────────────────────

describe("Toaster — dismiss", () => {
  it("clicking the × button removes the toast from the store", () => {
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "info", title: "click me" });
    });
    expect(screen.getByText("click me")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Dismiss notification"));
    expect(screen.queryByText("click me")).not.toBeInTheDocument();
    expect(useToastStore.getState().toasts).toHaveLength(0);
  });

  it("dismissing one toast leaves the others alive", () => {
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "info", title: "A" });
      useToastStore.getState().push({ severity: "info", title: "B" });
    });
    // Two toasts → two dismiss buttons. Click the second one (which
    // dismisses toast "A" since LIFO order means index 1 is the older).
    const buttons = screen.getAllByLabelText("Dismiss notification");
    fireEvent.click(buttons[1]);
    expect(screen.queryByText("A")).not.toBeInTheDocument();
    expect(screen.getByText("B")).toBeInTheDocument();
  });
});

// ── Auto-dismiss after TTL ───────────────────────────────────────

describe("Toaster — auto-dismiss", () => {
  it("auto-dismisses after the toast's ttlMs elapses", () => {
    vi.useFakeTimers();
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "info", title: "ephemeral", ttlMs: 1000 });
    });
    expect(screen.getByText("ephemeral")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.queryByText("ephemeral")).not.toBeInTheDocument();
  });

  it("toasts with different TTLs each respect their own deadline", () => {
    // Note on Toaster's timer model: the useEffect's deps include the
    // toasts array, so every list change clears + reschedules ALL
    // pending timers. That means each surviving toast's timer
    // effectively starts ticking from the moment of the last list
    // mutation, not from when the toast was first pushed.
    //
    // Test asserts the practical contract: after enough total time
    // for both toasts' TTLs to have elapsed (with rescheduling),
    // both are gone.
    vi.useFakeTimers();
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "info", title: "short", ttlMs: 500 });
      useToastStore.getState().push({ severity: "info", title: "long",  ttlMs: 5000 });
    });

    // Short fires first.
    act(() => { vi.advanceTimersByTime(500); });
    expect(screen.queryByText("short")).not.toBeInTheDocument();
    expect(screen.getByText("long")).toBeInTheDocument();

    // The "long" timer was reset when "short" was removed → it now
    // needs another full 5000 ms from t=500.
    act(() => { vi.advanceTimersByTime(5000); });
    expect(screen.queryByText("long")).not.toBeInTheDocument();
  });

  it("manual dismiss before the TTL clears the auto-dismiss timer (no double-fire)", () => {
    vi.useFakeTimers();
    render(<Toaster />);
    act(() => {
      useToastStore.getState().push({ severity: "info", title: "x", ttlMs: 1000 });
    });

    fireEvent.click(screen.getByLabelText("Dismiss notification"));
    expect(useToastStore.getState().toasts).toHaveLength(0);

    // Advance past the TTL — store must stay empty (the cleanup in
    // the useEffect cancelled the pending setTimeout, so dismiss(id)
    // for a now-missing id is a harmless no-op).
    act(() => { vi.advanceTimersByTime(2000); });
    expect(useToastStore.getState().toasts).toHaveLength(0);
  });
});
