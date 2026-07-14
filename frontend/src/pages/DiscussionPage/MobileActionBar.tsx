/**
 * Mobile sticky bottom action bar (<lg) — hosts the most-used CTAs
 * (Save / Run / Conclude / More) so users don't have to open the config
 * drawer for the common path; honours iOS home-bar safe-area inset, with
 * streamError pinned above. Pure display of the outer page state;
 * extracted verbatim from DiscussionPage (R7/G8 split). Zero hooks — the
 * shared `actionsBarProps` bundle is threaded down as a prop.
 */
import type { ComponentProps } from "react";

import { DiscussionActionsBar } from "@/components/discussion/DiscussionActionsBar";

export function MobileActionBar({
  streamError,
  actionsBarProps,
}: {
  streamError: string | null;
  actionsBarProps: Omit<ComponentProps<typeof DiscussionActionsBar>, "compact">;
}) {
  return (
    <div
      className="lg:hidden border-t border-border bg-card/95 backdrop-blur p-3 shrink-0"
      style={{ paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)" }}
    >
      {streamError && (
        <p className="text-meta text-danger bg-danger/10 border border-danger/30 rounded px-2 py-1 mb-2">
          {streamError}
        </p>
      )}
      <DiscussionActionsBar compact {...actionsBarProps} />
    </div>
  );
}
