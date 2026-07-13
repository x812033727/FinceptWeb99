/**
 * Mobile header (<lg only) — sessions drawer trigger, active discussion
 * title + status line, config drawer trigger. Pure display of the outer
 * page state; extracted verbatim from DiscussionPage (R7/G8 split). Zero
 * hooks — `t` is threaded down as a prop so all hooks stay in the parent.
 */
import type { Dispatch, SetStateAction } from "react";
import type { TFunction } from "i18next";
import { Folder, Settings as SettingsIcon } from "lucide-react";

import { DiscussionStatusBadge } from "@/components/discussion/DiscussionStatusBadge";
import type { DiscussionDetail } from "@/types/discussion";

export function MobileHeader({
  t,
  setSessionsSheetOpen,
  activeTitle,
  detail,
  setConfigSheetOpen,
}: {
  t: TFunction;
  setSessionsSheetOpen: Dispatch<SetStateAction<boolean>>;
  activeTitle: string;
  detail: DiscussionDetail | undefined;
  setConfigSheetOpen: Dispatch<SetStateAction<boolean>>;
}) {
  return (
    <header className="lg:hidden border-b border-border px-3 py-2 flex items-center gap-2 shrink-0">
      <button
        type="button"
        onClick={() => setSessionsSheetOpen(true)}
        aria-label={t("discussion.sessions_drawer_title")}
        className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-accent/10 min-h-[36px] min-w-[36px] inline-flex items-center justify-center"
      >
        <Folder className="h-4 w-4" aria-hidden="true" />
      </button>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-foreground truncate">{activeTitle}</p>
        {detail && (
          <p className="text-[10px] text-muted-foreground flex items-center gap-1.5">
            <DiscussionStatusBadge status={detail.status} />
            <span>R{detail.current_round}</span>
            {detail.as_of_date && <span>· {t("discussion.session_backtest_prefix", { date: detail.as_of_date })}</span>}
          </p>
        )}
      </div>
      <button
        type="button"
        onClick={() => setConfigSheetOpen(true)}
        aria-label={t("discussion.config_drawer_title")}
        className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-accent/10 min-h-[36px] min-w-[36px] inline-flex items-center justify-center"
      >
        <SettingsIcon className="h-4 w-4" aria-hidden="true" />
      </button>
    </header>
  );
}
