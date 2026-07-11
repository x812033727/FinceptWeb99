import { useTranslation } from "react-i18next";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import type { StrategyTemplate } from "./_helpers";

type Status = "active" | "frozen" | "shadow";

/**
 * PR-4c: per-strategy persona status grid.
 *
 * Renders one chip per persona on the strategy roster, color-coded
 * by status. Operators flip a persona between active / frozen /
 * shadow with a click on the dropdown next to each chip.
 *
 * Auto-freeze decisions made by the sweep Phase 3 hook show up
 * here on the next refetch (TanStack Query staleTime keeps the
 * UI close to live).
 */
export function PersonaStatusGrid({
  strategy,
  personaName,
}: {
  strategy: StrategyTemplate;
  personaName?: (id: string) => string;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const setStatus = useMutation({
    mutationFn: async ({
      persona_id, status,
    }: {
      persona_id: string;
      status: Status;
    }) => {
      const { data } = await api.patch(
        `/discussion/strategies/${strategy.id}/personas/${persona_id}/status`,
        { status },
      );
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategies"] });
    },
  });

  const statusFor = (pid: string): Status =>
    (strategy.persona_status?.[pid] as Status | undefined) ?? "active";

  // Hide entirely when there's nothing useful to show — fresh
  // strategy with everyone active = noise. Surface the row only
  // when something is non-active.
  const hasNonActive = strategy.persona_ids.some(
    (pid) => statusFor(pid) !== "active",
  );
  if (!hasNonActive) return null;

  return (
    <div className="border-t border-border/50 mt-2 pt-2 space-y-1">
      <div className="text-[11px] text-muted-foreground">
        {t("discussion.persona_status.title")}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {strategy.persona_ids.map((pid) => {
          const status = statusFor(pid);
          if (status === "active") return null;   // active = no UI noise
          const tone =
            status === "frozen"
              ? "bg-zinc-800/60 text-zinc-400 border-zinc-700/50 line-through"
              : "bg-purple-900/40 text-purple-200 border-purple-700/50";
          const display = personaName ? personaName(pid) : pid;
          return (
            <span
              key={pid}
              className={`inline-flex items-center gap-1 px-2 py-0.5 text-[11px]
                          rounded border ${tone}`}
              title={t(`discussion.persona_status.tooltip.${status}`)}
            >
              <span>
                {status === "frozen" ? "🧊" : "👻"}
              </span>
              <span>{display}</span>
              <button
                type="button"
                onClick={() => setStatus.mutate({
                  persona_id: pid, status: "active",
                })}
                disabled={setStatus.isPending}
                className="ml-1 text-[10px] text-muted-foreground
                           hover:text-success disabled:opacity-50"
              >
                {t("discussion.persona_status.thaw")}
              </button>
            </span>
          );
        })}
      </div>
    </div>
  );
}
