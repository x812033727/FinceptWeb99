import { useState } from "react";
import { useTranslation } from "react-i18next";

/** PR-D1 rule-engine alert creation form.
 *
 * The condition-type select drives a dynamic params sub-form (target
 * price / pct / lookback days / avg-volume multiple / streak days),
 * and the trigger-frequency select folds `repeat` + `cooldown_seconds`
 * into one control: once → fire-once-then-disable (legacy behavior),
 * 5min/1h/1d → repeat with the matching cooldown.
 */

export type Market = "US" | "TW" | "CRYPTO";

export type ConditionType =
  | "price_above"
  | "price_below"
  | "pct_change_above"
  | "pct_change_below"
  | "breakout_high"
  | "breakout_low"
  | "volume_surge"
  | "foreign_net_buy_streak";

export interface AlertRulePayload {
  symbol: string;
  market: Market;
  condition_type: ConditionType;
  target_price: number | null;
  params: Record<string, number> | null;
  repeat: boolean;
  cooldown_seconds: number;
}

const CONDITION_TYPES: ConditionType[] = [
  "price_above",
  "price_below",
  "pct_change_above",
  "pct_change_below",
  "breakout_high",
  "breakout_low",
  "volume_surge",
  "foreign_net_buy_streak",
];

const TW_ONLY: ConditionType[] = ["foreign_net_buy_streak"];

type Freq = "once" | "5m" | "1h" | "1d";
const FREQ_TO_COOLDOWN: Record<Freq, number> = {
  once: 0,
  "5m": 300,
  "1h": 3600,
  "1d": 86400,
};

interface Props {
  onSubmit: (body: AlertRulePayload) => void;
  isPending: boolean;
  /** Server-side error (mutation failure), rendered under the form. */
  serverError?: string;
}

export default function AlertBuilder({ onSubmit, isPending, serverError }: Props) {
  const { t } = useTranslation();
  const [symbol, setSymbol] = useState("");
  const [market, setMarket] = useState<Market>("US");
  const [conditionType, setConditionType] = useState<ConditionType>("price_above");
  const [targetPrice, setTargetPrice] = useState("");
  const [pct, setPct] = useState("");
  const [lookbackDays, setLookbackDays] = useState("20");
  const [multiple, setMultiple] = useState("2");
  const [streakDays, setStreakDays] = useState("3");
  const [freq, setFreq] = useState<Freq>("once");
  const [error, setError] = useState("");

  const visibleTypes = CONDITION_TYPES.filter(
    (ct) => market === "TW" || !TW_ONLY.includes(ct)
  );

  function handleMarketChange(next: Market) {
    setMarket(next);
    if (next !== "TW" && TW_ONLY.includes(conditionType)) {
      setConditionType("price_above");
    }
  }

  function buildPayload(): AlertRulePayload | null {
    const base = {
      symbol: symbol.trim().toUpperCase(),
      market,
      condition_type: conditionType,
      repeat: freq !== "once",
      cooldown_seconds: FREQ_TO_COOLDOWN[freq],
    };
    switch (conditionType) {
      case "price_above":
      case "price_below": {
        const price = parseFloat(targetPrice);
        if (!(price > 0)) return null;
        return { ...base, target_price: price, params: null };
      }
      case "pct_change_above":
      case "pct_change_below": {
        const v = parseFloat(pct);
        if (Number.isNaN(v)) return null;
        return { ...base, target_price: null, params: { pct: v } };
      }
      case "breakout_high":
      case "breakout_low": {
        const days = parseInt(lookbackDays, 10);
        if (!(days >= 2)) return null;
        return { ...base, target_price: null, params: { lookback_days: days } };
      }
      case "volume_surge": {
        const m = parseFloat(multiple);
        const days = parseInt(lookbackDays, 10);
        if (!(m > 1) || !(days >= 2)) return null;
        return {
          ...base,
          target_price: null,
          params: { multiple: m, lookback_days: days },
        };
      }
      case "foreign_net_buy_streak": {
        const days = parseInt(streakDays, 10);
        if (!(days >= 2)) return null;
        return { ...base, target_price: null, params: { days } };
      }
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!symbol.trim()) {
      setError(t("alerts.symbol"));
      return;
    }
    const payload = buildPayload();
    if (!payload) {
      setError(t("alerts.invalid_params"));
      return;
    }
    setError("");
    onSubmit(payload);
  }

  const inputCls =
    "w-full bg-background border border-border rounded px-2 py-1.5 text-sm";
  const labelCls = "text-xs text-muted-foreground";

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-card border border-border rounded-lg p-4 space-y-3"
    >
      <h2 className="text-sm font-medium">{t("alerts.create")}</h2>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="space-y-1">
          <label className={labelCls} htmlFor="ab-symbol">{t("alerts.symbol")}</label>
          <input
            id="ab-symbol"
            className={inputCls}
            placeholder="AAPL"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <label className={labelCls} htmlFor="ab-market">{t("alerts.market")}</label>
          <select
            id="ab-market"
            className={inputCls}
            value={market}
            onChange={(e) => handleMarketChange(e.target.value as Market)}
          >
            <option value="US">US</option>
            <option value="TW">TW</option>
            <option value="CRYPTO">CRYPTO</option>
          </select>
        </div>
        <div className="space-y-1 col-span-2">
          <label className={labelCls} htmlFor="ab-condition-type">
            {t("alerts.condition_type")}
          </label>
          <select
            id="ab-condition-type"
            className={inputCls}
            value={conditionType}
            onChange={(e) => setConditionType(e.target.value as ConditionType)}
          >
            {visibleTypes.map((ct) => (
              <option key={ct} value={ct}>
                {t(`alerts.type_${ct}`)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Dynamic params sub-form */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {(conditionType === "price_above" || conditionType === "price_below") && (
          <div className="space-y-1">
            <label className={labelCls} htmlFor="ab-target-price">
              {t("alerts.target_price")}
            </label>
            <input
              id="ab-target-price"
              type="number"
              min="0"
              step="0.01"
              className={inputCls}
              value={targetPrice}
              onChange={(e) => setTargetPrice(e.target.value)}
            />
          </div>
        )}
        {(conditionType === "pct_change_above" ||
          conditionType === "pct_change_below") && (
          <div className="space-y-1">
            <label className={labelCls} htmlFor="ab-pct">{t("alerts.param_pct")}</label>
            <input
              id="ab-pct"
              type="number"
              step="0.1"
              className={inputCls}
              placeholder="5"
              value={pct}
              onChange={(e) => setPct(e.target.value)}
            />
          </div>
        )}
        {(conditionType === "breakout_high" ||
          conditionType === "breakout_low" ||
          conditionType === "volume_surge") && (
          <div className="space-y-1">
            <label className={labelCls} htmlFor="ab-lookback">
              {t("alerts.param_lookback_days")}
            </label>
            <input
              id="ab-lookback"
              type="number"
              min="2"
              max="252"
              step="1"
              className={inputCls}
              value={lookbackDays}
              onChange={(e) => setLookbackDays(e.target.value)}
            />
          </div>
        )}
        {conditionType === "volume_surge" && (
          <div className="space-y-1">
            <label className={labelCls} htmlFor="ab-multiple">
              {t("alerts.param_multiple")}
            </label>
            <input
              id="ab-multiple"
              type="number"
              min="1"
              step="0.5"
              className={inputCls}
              value={multiple}
              onChange={(e) => setMultiple(e.target.value)}
            />
          </div>
        )}
        {conditionType === "foreign_net_buy_streak" && (
          <div className="space-y-1">
            <label className={labelCls} htmlFor="ab-streak-days">
              {t("alerts.param_days")}
            </label>
            <input
              id="ab-streak-days"
              type="number"
              min="2"
              max="60"
              step="1"
              className={inputCls}
              value={streakDays}
              onChange={(e) => setStreakDays(e.target.value)}
            />
          </div>
        )}
        <div className="space-y-1">
          <label className={labelCls} htmlFor="ab-freq">{t("alerts.freq")}</label>
          <select
            id="ab-freq"
            className={inputCls}
            value={freq}
            onChange={(e) => setFreq(e.target.value as Freq)}
          >
            <option value="once">{t("alerts.freq_once")}</option>
            <option value="5m">{t("alerts.freq_5m")}</option>
            <option value="1h">{t("alerts.freq_1h")}</option>
            <option value="1d">{t("alerts.freq_1d")}</option>
          </select>
        </div>
      </div>

      {(error || serverError) && (
        <p className="text-xs text-danger">{error || serverError}</p>
      )}
      <button
        type="submit"
        disabled={isPending}
        className="px-4 py-1.5 rounded bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
      >
        {isPending ? t("common.saving") : t("alerts.create")}
      </button>
    </form>
  );
}
