import { useTranslation } from "react-i18next";
import type { DiscussionMarket } from "@/types/discussion";
import type { StrategyFormState } from "./shared";

export function StrategyFormBlock({
  form, setForm, onSubmit, isSubmitting, error,
}: {
  form: StrategyFormState;
  setForm: (f: StrategyFormState) => void;
  onSubmit: () => void;
  isSubmitting: boolean;
  error: unknown;
}) {
  const { t } = useTranslation();
  return (
    <div className="bg-secondary/20 border border-border rounded-lg p-3 space-y-2 text-xs">
      <label className="flex flex-col gap-1">
        <span className="text-muted-foreground">
          {t("strategy.name", "策略名稱")}
        </span>
        <input
          value={form.name}
          maxLength={120}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          className="bg-background border border-border rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-muted-foreground">
          {t("strategy.description", "描述（選填）")}
        </span>
        <input
          value={form.description}
          onChange={(e) => setForm({ ...form, description: e.target.value })}
          className="bg-background border border-border rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-muted-foreground">
          {t("strategy.topic", "題目")}
        </span>
        <textarea
          rows={2}
          value={form.topic}
          onChange={(e) => setForm({ ...form, topic: e.target.value })}
          className="bg-background border border-border rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-muted-foreground">
          {t("strategy.rules", "規則")}
        </span>
        <textarea
          rows={2}
          value={form.rules}
          onChange={(e) => setForm({ ...form, rules: e.target.value })}
          className="bg-background border border-border rounded px-2 py-1"
        />
      </label>
      <div className="grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            {t("strategy.market", "市場")}
          </span>
          <select
            value={form.market}
            onChange={(e) =>
              setForm({ ...form, market: e.target.value as DiscussionMarket })
            }
            className="bg-background border border-border rounded px-2 py-1"
          >
            <option value="TW">TW</option>
            <option value="US">US</option>
            <option value="GLOBAL">GLOBAL</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            {t("strategy.persona_ids", "專家 IDs（逗號分隔）")}
          </span>
          <input
            value={form.personaIdsCsv}
            onChange={(e) => setForm({ ...form, personaIdsCsv: e.target.value })}
            placeholder="bull, bear, quant"
            className="bg-background border border-border rounded px-2 py-1"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            {t("strategy.default_rounds", "預設輪數")}
          </span>
          <input
            type="number" min={1} max={5}
            value={form.defaultRounds}
            onChange={(e) =>
              setForm({ ...form, defaultRounds: Number(e.target.value) })
            }
            className="bg-background border border-border rounded px-2 py-1"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-muted-foreground">
            {t("strategy.default_concurrency", "預設並行")}
          </span>
          <input
            type="number" min={1} max={3}
            value={form.defaultConcurrency}
            onChange={(e) =>
              setForm({ ...form, defaultConcurrency: Number(e.target.value) })
            }
            className="bg-background border border-border rounded px-2 py-1"
          />
        </label>
      </div>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={form.defaultAutoPostMortem}
          onChange={(e) =>
            setForm({ ...form, defaultAutoPostMortem: e.target.checked })
          }
        />
        <span className="text-muted-foreground">
          {t("strategy.default_auto_post_mortem", "預設啟用事後檢討")}
        </span>
      </label>

      <fieldset className="border border-border rounded p-2 mt-1 space-y-1.5">
        <legend className="text-micro text-muted-foreground uppercase tracking-wider px-1">
          {t("strategy.auto_schedule", "自動排程（PR-D）")}
        </legend>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={form.autoScheduleEnabled}
            onChange={(e) =>
              setForm({ ...form, autoScheduleEnabled: e.target.checked })
            }
          />
          <span className="text-muted-foreground">
            {t("strategy.auto_schedule_enabled",
               "啟用後依下列設定自動 launch sweep")}
          </span>
        </label>
        {form.autoScheduleEnabled && (
          <div className="grid grid-cols-3 gap-2">
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground">
                {t("strategy.cadence_hours", "間隔（小時）")}
              </span>
              <input
                type="number" min={1} max={720}
                value={form.autoScheduleCadenceHours}
                onChange={(e) => setForm({
                  ...form,
                  autoScheduleCadenceHours: Number(e.target.value),
                })}
                className="bg-background border border-border rounded px-2 py-1"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground"
                    title={t("strategy.anchor_offset_tip",
                             "0=今天，-1=昨天")}>
                {t("strategy.anchor_offset", "錨點偏移（天）")}
              </span>
              <input
                type="number" min={-30} max={0}
                value={form.autoScheduleAnchorOffsetDays}
                onChange={(e) => setForm({
                  ...form,
                  autoScheduleAnchorOffsetDays: Number(e.target.value),
                })}
                className="bg-background border border-border rounded px-2 py-1"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground">
                {t("strategy.auto_trading_days", "每次掃幾日")}
              </span>
              <input
                type="number" min={1} max={30}
                value={form.autoScheduleTradingDaysCount}
                onChange={(e) => setForm({
                  ...form,
                  autoScheduleTradingDaysCount: Number(e.target.value),
                })}
                className="bg-background border border-border rounded px-2 py-1"
              />
            </label>
          </div>
        )}
      </fieldset>
      {error ? (
        <p className="text-meta text-danger">
          {(error as { response?: { data?: { detail?: string } } })
            ?.response?.data?.detail ?? (error as Error).message}
        </p>
      ) : null}
      <button
        type="button"
        onClick={onSubmit}
        disabled={isSubmitting}
        className="bg-primary hover:bg-primary/90 text-white text-xs font-semibold px-3 py-1.5 rounded disabled:opacity-50"
      >
        {isSubmitting
          ? t("common.saving", "儲存中…")
          : t("strategy.save", "儲存策略")}
      </button>
    </div>
  );
}
