import { useTranslation } from "react-i18next";
import {
  OVERLAY_KEYS,
  SUB_KEYS,
  type IndicatorPrefs,
  type OverlayKey,
  type SubKey,
} from "./indicatorPrefs";

function Chip({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`px-2 py-0.5 text-[11px] rounded transition-colors touch-manipulation ${
        active
          ? "bg-primary text-primary-foreground"
          : "text-muted-foreground hover:text-foreground hover:bg-accent/20"
      }`}
    >
      {label}
    </button>
  );
}

interface Props {
  prefs: IndicatorPrefs;
  onChange: (prefs: IndicatorPrefs) => void;
}

/**
 * Compact indicator toggle row rendered above the candlestick chart.
 * MA / EMA / BOLL overlay independently on the main pane; RSI / MACD / KD
 * are mutually exclusive in the sub-pane (re-tapping the active one, or
 * tapping 「無」, turns the sub-pane off).
 */
export default function IndicatorToolbar({ prefs, onChange }: Props) {
  const { t } = useTranslation();

  const toggleOverlay = (key: OverlayKey) => {
    const overlays = prefs.overlays.includes(key)
      ? prefs.overlays.filter((k) => k !== key)
      : [...prefs.overlays, key];
    onChange({ ...prefs, overlays });
  };

  const selectSub = (key: SubKey | null) => {
    onChange({ ...prefs, sub: key === prefs.sub ? null : key });
  };

  return (
    <div className="flex flex-wrap items-center gap-0.5 pb-1 text-xs">
      <span className="sr-only">{t("chart.ind_main_group")}</span>
      {OVERLAY_KEYS.map((k) => (
        <Chip
          key={k}
          active={prefs.overlays.includes(k)}
          label={t(`chart.ind_${k}`)}
          onClick={() => toggleOverlay(k)}
        />
      ))}
      <span aria-hidden className="mx-1 h-3.5 w-px bg-border" />
      <span className="sr-only">{t("chart.ind_sub_group")}</span>
      <Chip active={prefs.sub === null} label={t("chart.ind_none")} onClick={() => selectSub(null)} />
      {SUB_KEYS.map((k) => (
        <Chip
          key={k}
          active={prefs.sub === k}
          label={t(`chart.ind_${k}`)}
          onClick={() => selectSub(k)}
        />
      ))}
    </div>
  );
}
