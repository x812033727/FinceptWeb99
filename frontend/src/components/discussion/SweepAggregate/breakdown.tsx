/**
 * SweepAggregate per-persona breakdown: the hit-rate leaderboard
 * table (sorted by hit rate, desc) and the collapsible post-mortem
 * lessons feed. Both render nothing when their underlying list is
 * empty.
 */
import { useTranslation } from "react-i18next";
import type { SweepAggregate } from "../_helpers";
import { DataTable, type DataTableColumn } from "../../ui/table";

export function PersonaTable({ agg }: { agg: SweepAggregate }) {
  const { t } = useTranslation();
  if (agg.per_persona.length === 0) return null;
  const sorted = [...agg.per_persona].sort((a, b) => {
    const ar = a.hit_rate ?? -1;
    const br = b.hit_rate ?? -1;
    return br - ar;
  });
  type PersonaRow = (typeof agg.per_persona)[number];
  const columns: DataTableColumn<PersonaRow>[] = [
    {
      key: "persona",
      header: t("aggregate.persona", "專家"),
      mobile: "primary",
      cellClassName: "font-mono truncate max-w-[8em]",
      render: (p) => p.persona_id,
    },
    {
      key: "hit",
      header: t("aggregate.hit", "命中"),
      numeric: true,
      render: (p) =>
        p.hit_rate === null ? "—" : `${(p.hit_rate * 100).toFixed(0)}%`,
    },
    {
      key: "win",
      header: t("aggregate.win_count", "勝"),
      numeric: true,
      render: (p) => <span className="text-success">{p.win_count}</span>,
    },
    {
      key: "disc",
      header: t("aggregate.disc_count", "場次"),
      numeric: true,
      render: (p) => p.discussions_count,
    },
    {
      key: "ad",
      header: (
        <span title={t("aggregate.agree_dissent_tip", "agree+supplement / dissent")}>
          A/D
        </span>
      ),
      align: "right",
      cellClassName: "font-mono text-muted-foreground",
      render: (p) => `${p.agree_turn_count}/${p.dissent_turn_count}`,
    },
  ];
  return (
    <div className="bg-secondary/20 border border-border rounded p-2">
      <p className="text-micro text-muted-foreground uppercase tracking-wider mb-1">
        {t("aggregate.persona_stats", "專家命中表")}
      </p>
      <DataTable
        columns={columns}
        rows={sorted}
        rowKey={(p) => p.persona_id}
        mobileMode="cards"
        aria-label={t("aggregate.persona_stats", "專家命中表")}
      />
    </div>
  );
}

export function LessonsList({ agg }: { agg: SweepAggregate }) {
  const { t } = useTranslation();
  if (agg.lessons.length === 0) return null;
  return (
    <details className="bg-secondary/20 border border-border rounded p-2">
      <summary className="text-micro text-muted-foreground uppercase tracking-wider cursor-pointer">
        {t("aggregate.lessons", "事後檢討教訓")}（{agg.lessons.length}）
      </summary>
      <ul className="mt-1.5 space-y-1">
        {agg.lessons.map((l, i) => (
          <li key={i} className="text-[11px]">
            <span className="inline-block bg-warning/10 text-warning border border-warning/30 rounded px-1 py-0.5 mr-1.5 text-[9px] uppercase">
              {l.category}
            </span>
            <span className="text-muted-foreground mr-1">{l.as_of_date}</span>
            <span>{l.lesson_text}</span>
            {l.related_symbols.length > 0 && (
              <span className="ml-1 text-muted-foreground font-mono">
                [{l.related_symbols.join(", ")}]
              </span>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}
