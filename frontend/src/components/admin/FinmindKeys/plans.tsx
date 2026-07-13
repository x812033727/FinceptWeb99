import type { Dispatch, SetStateAction } from "react";
import type { UseMutationResult, UseQueryResult } from "@tanstack/react-query";

import { DataTable, type DataTableColumn } from "../../ui/table";
import type { PlanFormState, PlanItem, PlanUpsertInput } from "./types";

/**
 * Plans section (above the issue form so the operator creates a plan
 * before issuing keys to use it). Pure display: the plan mini-form's
 * state + the upsert mutation live in the entry card and arrive here
 * as props.
 */
export function PlansSection({
  planForm,
  setPlanForm,
  upsertPlanMutation,
  plansQuery,
  planColumns,
}: {
  planForm: PlanFormState;
  setPlanForm: Dispatch<SetStateAction<PlanFormState>>;
  upsertPlanMutation: UseMutationResult<PlanItem, Error, PlanUpsertInput>;
  plansQuery: UseQueryResult<PlanItem[], Error>;
  planColumns: DataTableColumn<PlanItem>[];
}) {
  return (
    <div className="rounded border border-border bg-muted/10 p-3">
      <h3 className="mb-2 text-sm font-semibold">Plans</h3>
      <form
        className="flex flex-wrap items-end gap-2 text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          if (!planForm.code.trim() || !planForm.name.trim()) return;
          upsertPlanMutation.mutate({
            code: planForm.code.trim(),
            name: planForm.name.trim(),
            price_monthly: planForm.price_monthly
              ? Number(planForm.price_monthly)
              : null,
            quota_daily_calls: Number(planForm.quota_daily_calls) || 0,
            quota_daily_rows: Number(planForm.quota_daily_rows) || 0,
          });
        }}
      >
        <label className="flex flex-col">
          <span className="text-xs text-muted-foreground">Code</span>
          <input
            type="text"
            required
            value={planForm.code}
            onChange={(e) =>
              setPlanForm({ ...planForm, code: e.target.value })
            }
            className="w-24 rounded border border-border bg-background px-2 py-1"
            placeholder="pro"
          />
        </label>
        <label className="flex flex-col">
          <span className="text-xs text-muted-foreground">Name</span>
          <input
            type="text"
            required
            value={planForm.name}
            onChange={(e) =>
              setPlanForm({ ...planForm, name: e.target.value })
            }
            className="rounded border border-border bg-background px-2 py-1"
            placeholder="Pro"
          />
        </label>
        <label className="flex flex-col">
          <span className="text-xs text-muted-foreground">
            TWD/month
          </span>
          <input
            type="number"
            value={planForm.price_monthly}
            onChange={(e) =>
              setPlanForm({
                ...planForm,
                price_monthly: e.target.value,
              })
            }
            className="w-24 rounded border border-border bg-background px-2 py-1"
            placeholder="990"
          />
        </label>
        <label className="flex flex-col">
          <span className="text-xs text-muted-foreground">
            Calls/day
          </span>
          <input
            type="number"
            value={planForm.quota_daily_calls}
            onChange={(e) =>
              setPlanForm({
                ...planForm,
                quota_daily_calls: e.target.value,
              })
            }
            className="w-24 rounded border border-border bg-background px-2 py-1"
          />
        </label>
        <label className="flex flex-col">
          <span className="text-xs text-muted-foreground">
            Rows/day
          </span>
          <input
            type="number"
            value={planForm.quota_daily_rows}
            onChange={(e) =>
              setPlanForm({
                ...planForm,
                quota_daily_rows: e.target.value,
              })
            }
            className="w-28 rounded border border-border bg-background px-2 py-1"
          />
        </label>
        <button
          type="submit"
          disabled={upsertPlanMutation.isPending}
          className="rounded border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
        >
          {upsertPlanMutation.isPending ? "Saving…" : "Save plan"}
        </button>
      </form>

      {plansQuery.data && plansQuery.data.length > 0 && (
        <DataTable
          className="mt-3"
          columns={planColumns}
          rows={plansQuery.data}
          rowKey={(p) => p.code}
          mobileMode="scroll"
          aria-label="Plans"
        />
      )}
    </div>
  );
}
