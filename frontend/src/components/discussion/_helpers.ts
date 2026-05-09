/**
 * Re-export shim. The 38 importers across the discussion component
 * group + DiscussionPage all use `from "./_helpers"` — keeping that
 * facade intact lets the underlying split into `_storage`, `_api`,
 * `_format` happen without touching call sites.
 *
 * New code should import from the concrete sibling module instead
 * (`./_storage`, `./_api`, `./_format`) — that's where the symbol
 * actually lives. This file only exists for backward compatibility
 * with the pre-split layout and can be deleted once every call site
 * has migrated.
 */
export * from "./_storage";
export * from "./_api";
export * from "./_format";
