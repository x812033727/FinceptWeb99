/**
 * Desktop sticky action bar (lg+, PR-D) — Run / Conclude / More stay one
 * click away regardless of transcript scroll, with streamError pinned
 * above so rate limits stay visible. Pure display of the outer page
 * state; extracted verbatim from DiscussionPage (R7/G8 split). Zero
 * hooks — the shared `actionsBarProps` bundle is threaded down as a prop.
 */
import type { ComponentProps } from "react";

import { DiscussionActionsBar } from "@/components/discussion/DiscussionActionsBar";

export function DesktopActionBar({
  streamError,
  actionsBarProps,
}: {
  streamError: string | null;
  actionsBarProps: Omit<ComponentProps<typeof DiscussionActionsBar>, "compact">;
}) {
  return (
    <div className="hidden lg:block sticky top-0 z-10 border-b border-border bg-card/95 backdrop-blur px-4 py-2 shrink-0">
      {streamError && (
        <p className="text-xs text-danger bg-danger/10 border border-danger/30 rounded px-2 py-1 mb-2">
          {streamError}
        </p>
      )}
      <DiscussionActionsBar {...actionsBarProps} />
    </div>
  );
}
