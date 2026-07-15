import { useTranslation } from "react-i18next";

import type { TWSecurityMaster } from "./_shared";

interface Props {
  rule: TWSecurityMaster;
}

export function SecurityRuleCard({ rule }: Props) {
  const { t } = useTranslation();
  const sourceUrl = rule.tax_source_url ?? rule.classification_source_url;
  const typeKey = `stock.security_rule.type_${rule.instrument_type}`;

  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3 text-xs">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <span className="font-medium text-foreground">
          {t("stock.security_rule.title")}
        </span>
        <span className="rounded bg-accent/15 px-2 py-0.5 text-accent-foreground">
          {t(typeKey, { defaultValue: rule.instrument_type })}
        </span>
        {rule.is_leveraged && <span>{t("stock.security_rule.leveraged")}</span>}
        {rule.is_inverse && <span>{t("stock.security_rule.inverse")}</span>}
        <span>
          {t("stock.security_rule.board_lot", { value: rule.board_lot_size })}
        </span>
        <span>
          {t("stock.security_rule.sell_tax", {
            value: (rule.sell_tax_bps / 100).toFixed(2),
          })}
        </span>
        <span className="text-muted-foreground">
          {t("stock.security_rule.effective_from", { date: rule.effective_from })}
        </span>
        {rule.is_manual_override && (
          <span className="text-warning">{t("stock.security_rule.manual")}</span>
        )}
        {rule.fallback && (
          <span className="text-warning">{t("stock.security_rule.fallback")}</span>
        )}
        {sourceUrl && (
          <a
            className="ml-auto text-accent hover:underline"
            href={sourceUrl}
            target="_blank"
            rel="noreferrer"
          >
            {t("stock.security_rule.source")}
          </a>
        )}
      </div>
    </div>
  );
}
