#!/usr/bin/env bash
# Chain the remaining per-symbol backfills after the currently-running
# TaiwanStockPrice + TaiwanStockInstitutionalInvestorsBuySell finish.
# Each dataset runs sequentially against the equity universe (2535
# symbols, 10 years), respecting the FinMind 6000/hr cap (a single
# backfill steady-states at ~2,500 calls/hr).
set -uo pipefail

LOG=/var/log/finmind-backfill.log
DC=/usr/local/bin/docker-compose
EQUITY=/tmp/equity.txt
EQUITY_HOST=/opt/finceptweb/equity.txt
BACKEND_CT=finceptweb-backend-1

# The backend container's /tmp is wiped on every recreate (no
# bind mount), so a redeploy strands the chain mid-flight.
# Re-stage from the repo-tracked source-of-truth on every run.
if [[ -f "$EQUITY_HOST" ]]; then
    docker cp "$EQUITY_HOST" "${BACKEND_CT}:${EQUITY}" 2>>"$LOG" || true
else
    echo "=== CHAIN ABORT: $EQUITY_HOST missing ===" >> "$LOG"
    exit 1
fi

# Wait until both currently-running per-symbol datasets are no longer
# claiming chunks. `started_at < now() - 5 minutes` filter avoids
# treating freshly-claimed chunks of OTHER datasets as blocking.
wait_for_clear() {
    while true; do
        running=$(cd /opt/finceptweb && $DC exec -T postgres psql -U fincept -d finceptweb -tAc "
            SELECT count(*) FROM finmind.backfill_progress
             WHERE dataset_code IN ('TaiwanStockPrice','TaiwanStockInstitutionalInvestorsBuySell')
               AND status='running'
               AND started_at > now() - interval '10 minutes'
        " 2>/dev/null | tr -d ' \n')
        if [[ "$running" == "0" ]]; then return 0; fi
        sleep 120
    done
}

run_one() {
    local ds="$1"
    echo "=== CHAIN START $ds 10y ===" >> "$LOG"
    cd /opt/finceptweb && $DC exec -T backend python -m finmind.scripts.backfill \
        --dataset "$ds" --symbols-file "$EQUITY" --days 3650 >> "$LOG" 2>&1
    echo "=== CHAIN END   $ds ===" >> "$LOG"
}

# Wait for the existing pair to drop off
echo "=== CHAIN WAITING for TaiwanStockPrice + InstitutionalInvestorsBuySell to finish ===" >> "$LOG"
wait_for_clear

# Run the queue sequentially — high-value daily-grain per-symbol
# datasets, ordered by analytical priority. Each ~2-3 hours.
for ds in \
    TaiwanStockMarginPurchaseShortSale \
    TaiwanStockPriceAdj \
    TaiwanStockPER \
    TaiwanStockMarketValue \
    TaiwanStockSecuritiesLending \
    TaiwanDailyShortSaleBalances \
    TaiwanStockMonthRevenue \
    TaiwanStockBalanceSheet \
    TaiwanStockFinancialStatements \
    TaiwanStockCashFlowsStatement \
    TaiwanStockDividend
do
    run_one "$ds"
done

echo "=== CHAIN DONE ===" >> "$LOG"
