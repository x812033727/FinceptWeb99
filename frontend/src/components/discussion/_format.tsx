/**
 * Formatters + inline-markdown renderers + context summarizer +
 * markdown-export helpers for the discussion subsystem.
 *
 * Thin re-export facade: the implementation now lives split by domain
 * under `./format/*`. This file is kept so existing call sites (and the
 * `_helpers` re-export shim) continue to import from "./_format"
 * without edits. Kept as `.tsx` (matching the original path) even though
 * this barrel itself contains no JSX — the JSX lives in `./format/markdown`.
 *
 *   ./format/dates    — date formatting (Asia/Taipei)
 *   ./format/numbers  — price/percent formatting, band colour classes
 *   ./format/persona  — persona-name / avatar-glyph hooks
 *   ./format/symbols  — outcome-band classification + discussion titles
 *   ./format/markdown — inline-markdown render + ctx summarizer + export
 */
/* This is a pure re-export barrel (no component definitions), so the
   Fast-Refresh "only export components" heuristic doesn't apply. */
/* eslint-disable react-refresh/only-export-components */
export * from "./format/dates";
export * from "./format/numbers";
export * from "./format/persona";
export * from "./format/symbols";
export * from "./format/markdown";
