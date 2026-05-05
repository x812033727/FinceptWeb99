import { MoreHorizontal } from "lucide-react";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { TabButton } from "./_atoms";

export interface TabDef {
  key: string;
  label: string;
  active: boolean;
  onClick: () => void;
}

const VISIBLE_LIMIT = 4;

// Renders a horizontal tab strip that collapses to "first N + More
// dropdown" below the md breakpoint. The active tab is always kept
// in the visible slice — when it would otherwise sit in the overflow
// zone it gets swapped into the last visible position so the user
// always sees their current selection without opening the menu.
export function TabStrip({ tabs }: { tabs: TabDef[] }) {
  const isMdUp = useMediaQuery("(min-width: 768px)");

  let visible: TabDef[] = tabs;
  let overflow: TabDef[] = [];

  if (!isMdUp && tabs.length > VISIBLE_LIMIT) {
    const activeIdx = tabs.findIndex((t) => t.active);
    if (activeIdx >= VISIBLE_LIMIT) {
      visible = [...tabs.slice(0, VISIBLE_LIMIT - 1), tabs[activeIdx]];
      overflow = [
        ...tabs.slice(VISIBLE_LIMIT - 1, activeIdx),
        ...tabs.slice(activeIdx + 1),
      ];
    } else {
      visible = tabs.slice(0, VISIBLE_LIMIT);
      overflow = tabs.slice(VISIBLE_LIMIT);
    }
  }

  return (
    <div className="flex border-b border-border -mx-4 px-4 sm:mx-0 sm:px-0">
      {visible.map((t) => (
        <TabButton key={t.key} active={t.active} label={t.label} onClick={t.onClick} />
      ))}
      {overflow.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label="More tabs"
            className="shrink-0 px-3 py-2 text-sm border-b-2 border-transparent text-muted-foreground hover:text-foreground transition-colors min-h-[44px] flex items-center gap-1 ml-auto md:hidden"
          >
            <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[10rem]">
            {overflow.map((t) => (
              <DropdownMenuItem key={t.key} onSelect={t.onClick}>
                {t.label}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}
