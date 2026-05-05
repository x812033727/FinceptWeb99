import { useEffect, useState } from "react";

// Subscribes a component to a CSS media query. Returns the current
// match state and re-renders on transitions. SSR-safe (initial value
// is `false` when window is undefined). Used for breakpoint-aware
// behaviour that CSS alone can't express (e.g. picking a tab subset
// to show inline vs. collapse into an overflow menu).
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return false;
    }
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mql = window.matchMedia(query);
    const handler = (e: MediaQueryListEvent) => setMatches(e.matches);
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMatches(mql.matches);
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, [query]);

  return matches;
}
