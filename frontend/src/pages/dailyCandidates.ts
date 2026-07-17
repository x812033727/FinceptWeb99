export type PoolItem = { symbol?: string; name?: string; strategy_score?: number; signal_type?: string };

/** Keep the highest-scored entry per symbol, sorted by score descending. */
export function dedupeBySymbol(items: PoolItem[]): PoolItem[] {
  const best = new Map<string, PoolItem>();
  for (const item of items) {
    if (!item.symbol) continue;
    const prev = best.get(item.symbol);
    if (!prev || (item.strategy_score ?? -Infinity) > (prev.strategy_score ?? -Infinity)) best.set(item.symbol, item);
  }
  return [...best.values()].sort((a, b) => (b.strategy_score ?? -Infinity) - (a.strategy_score ?? -Infinity));
}
