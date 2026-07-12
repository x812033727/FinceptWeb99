/**
 * Shared base HTTP client for the discussion API modules. Re-exported
 * here so each resource-domain file (`sessions.ts`, `strategies.ts`,
 * `sweeps.ts`) pulls the base client from a single point instead of
 * importing `@/lib/api` independently.
 */
export { default as api } from "@/lib/api";
