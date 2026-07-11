import { useTranslation } from "react-i18next";

export function StatRow({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div className="flex justify-between items-center py-1.5 border-b border-border/50 last:border-0">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-xs text-foreground font-medium num">{value ?? "—"}</span>
    </div>
  );
}

export function TabButton({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`shrink-0 whitespace-nowrap px-3 sm:px-4 py-2 text-sm border-b-2 transition-colors min-h-[44px] ${
        active
          ? "border-primary text-foreground font-medium"
          : "border-transparent text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );
}

export function PeriodButton({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-1.5 sm:px-2.5 sm:py-1 text-xs rounded transition-colors touch-manipulation min-h-[36px] sm:min-h-0 ${
        active ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );
}

export function Loading() {
  const { t } = useTranslation();
  return <div className="p-8 text-center text-muted-foreground text-sm animate-pulse">{t("common.loading")}</div>;
}
