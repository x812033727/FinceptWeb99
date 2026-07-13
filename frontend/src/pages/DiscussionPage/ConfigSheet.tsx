/**
 * Mobile config Sheet (<lg) — right-side drawer carrying the same
 * Topic / Rules / Personas / Market form as the desktop inline panel.
 * Pure display of the outer page state; extracted verbatim from
 * DiscussionPage (R7/G8 split). Zero hooks — `t` and the shared
 * `configFormProps` bundle are threaded down as props.
 */
import type { ComponentProps, Dispatch, SetStateAction } from "react";
import type { TFunction } from "i18next";

import { DiscussionConfigForm } from "@/components/discussion/DiscussionConfigForm";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

export function ConfigSheet({
  configSheetOpen,
  setConfigSheetOpen,
  t,
  configFormProps,
}: {
  configSheetOpen: boolean;
  setConfigSheetOpen: Dispatch<SetStateAction<boolean>>;
  t: TFunction;
  configFormProps: ComponentProps<typeof DiscussionConfigForm>;
}) {
  return (
    <Sheet open={configSheetOpen} onOpenChange={setConfigSheetOpen}>
      <SheetContent side="right" className="w-96 max-w-[95vw] overflow-y-auto p-4 space-y-3">
        <SheetHeader>
          <SheetTitle>{t("discussion.config_drawer_title")}</SheetTitle>
          <SheetDescription>{t("discussion.config_drawer_hint")}</SheetDescription>
        </SheetHeader>
        <DiscussionConfigForm {...configFormProps} />
      </SheetContent>
    </Sheet>
  );
}
