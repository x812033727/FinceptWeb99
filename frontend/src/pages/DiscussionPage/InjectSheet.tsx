/**
 * Mobile inject Sheet (<lg) — bottom drawer triggered from the More
 * menu so users can drop a between-rounds / interject / followup message
 * without scrolling the config drawer. Pure display of the outer page
 * state; extracted verbatim from DiscussionPage (R7/G8 split). Zero
 * hooks — `t`, `injectMode`, and the shared `injectFormProps` bundle are
 * threaded down as props.
 */
import type { Dispatch, SetStateAction } from "react";
import type { TFunction } from "i18next";

import { InjectForm } from "@/components/discussion/InjectForm";
import type { InjectFormProps, InjectMode } from "@/components/discussion/InjectForm";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

export function InjectSheet({
  injectSheetOpen,
  setInjectSheetOpen,
  t,
  injectMode,
  injectFormProps,
}: {
  injectSheetOpen: boolean;
  setInjectSheetOpen: Dispatch<SetStateAction<boolean>>;
  t: TFunction;
  injectMode: InjectMode;
  injectFormProps: Omit<InjectFormProps, "mode">;
}) {
  return (
    <Sheet open={injectSheetOpen} onOpenChange={setInjectSheetOpen}>
      <SheetContent side="bottom" className="max-h-[60vh] overflow-y-auto p-4 space-y-3">
        <SheetHeader>
          <SheetTitle>
            {t(
              injectMode === "running"
                ? "discussion.interject_label"
                : injectMode === "followup"
                  ? "discussion.followup_label"
                  : "discussion.inject_label",
            )}
          </SheetTitle>
        </SheetHeader>
        <InjectForm mode={injectMode} {...injectFormProps} />
      </SheetContent>
    </Sheet>
  );
}
