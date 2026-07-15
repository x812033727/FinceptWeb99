/**
 * Pure technical-indicator math for the candlestick chart (feature A1).
 *
 * Every function returns arrays index-aligned with its input; positions
 * inside the warm-up window are `null` so callers can skip them when
 * feeding lightweight-charts (which rejects null values but accepts
 * omitted points). Inputs accept `null` entries too, which lets results
 * compose (e.g. the MACD signal line is an EMA of the MACD line, which
 * itself has leading nulls).
 *
 * Daily OHLCV series are small (≤ ~1300 points for 5y), so O(n·period)
 * loops are fine — clarity over micro-optimisation.
 */

export type Series = readonly (number | null)[];

/** Index of the first non-null entry, or -1 when all null/empty. */
function firstValid(values: Series): number {
  for (let i = 0; i < values.length; i++) if (values[i] != null) return i;
  return -1;
}

/** Simple moving average. Warm-up (first `period - 1` valid points) is null. */
export function sma(values: Series, period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  const start = firstValid(values);
  if (start < 0 || period < 1) return out;
  for (let i = start + period - 1; i < values.length; i++) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const v = values[j];
      if (v == null) return out; // non-leading gap: bail (shouldn't happen for OHLCV)
      sum += v;
    }
    out[i] = sum / period;
  }
  return out;
}

/**
 * Exponential moving average, seeded with the SMA of the first `period`
 * valid points (the standard convention), then k = 2 / (period + 1).
 */
export function ema(values: Series, period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  const start = firstValid(values);
  if (start < 0 || period < 1 || start + period > values.length) return out;
  let sum = 0;
  for (let i = start; i < start + period; i++) sum += values[i] as number;
  let prev = sum / period;
  out[start + period - 1] = prev;
  const k = 2 / (period + 1);
  for (let i = start + period; i < values.length; i++) {
    const v = values[i];
    if (v == null) break;
    prev = v * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

export interface BollingerResult {
  middle: (number | null)[];
  upper: (number | null)[];
  lower: (number | null)[];
}

/**
 * Bollinger Bands: middle = SMA(period), bands = middle ± mult · σ where σ
 * is the population standard deviation over the same window (the common
 * charting convention, e.g. TradingView).
 */
export function bollinger(values: Series, period = 20, mult = 2): BollingerResult {
  const middle = sma(values, period);
  const upper: (number | null)[] = new Array(values.length).fill(null);
  const lower: (number | null)[] = new Array(values.length).fill(null);
  for (let i = 0; i < values.length; i++) {
    const m = middle[i];
    if (m == null) continue;
    let sq = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const v = values[j] as number;
      sq += (v - m) * (v - m);
    }
    const sd = Math.sqrt(sq / period);
    upper[i] = m + mult * sd;
    lower[i] = m - mult * sd;
  }
  return { middle, upper, lower };
}

/**
 * Relative Strength Index with Wilder smoothing. First value appears at
 * index `period` (needs `period` price changes). All-gain windows → 100,
 * all-loss → 0.
 */
export function rsi(values: Series, period = 14): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  const start = firstValid(values);
  if (start < 0 || period < 1 || start + period >= values.length) return out;
  let avgGain = 0;
  let avgLoss = 0;
  for (let i = start + 1; i <= start + period; i++) {
    const prev = values[i - 1];
    const cur = values[i];
    if (prev == null || cur == null) return out;
    const d = cur - prev;
    if (d > 0) avgGain += d;
    else avgLoss -= d;
  }
  avgGain /= period;
  avgLoss /= period;
  const toRsi = (g: number, l: number) => (
    l === 0 ? (g > 0 ? 100 : 50) : 100 - 100 / (1 + g / l)
  );
  out[start + period] = toRsi(avgGain, avgLoss);
  for (let i = start + period + 1; i < values.length; i++) {
    const prev = values[i - 1];
    const cur = values[i];
    if (prev == null || cur == null) break;
    const d = cur - prev;
    avgGain = (avgGain * (period - 1) + Math.max(d, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-d, 0)) / period;
    out[i] = toRsi(avgGain, avgLoss);
  }
  return out;
}

export interface MACDResult {
  macd: (number | null)[];
  signal: (number | null)[];
  histogram: (number | null)[];
}

/** MACD line = EMA(fast) − EMA(slow); signal = EMA(signalPeriod) of the MACD line. */
export function macd(values: Series, fast = 12, slow = 26, signalPeriod = 9): MACDResult {
  const fastEma = ema(values, fast);
  const slowEma = ema(values, slow);
  const line: (number | null)[] = values.map((_, i) => {
    const f = fastEma[i];
    const s = slowEma[i];
    return f == null || s == null ? null : f - s;
  });
  const signal = ema(line, signalPeriod);
  const histogram = line.map((v, i) => {
    const s = signal[i];
    return v == null || s == null ? null : v - s;
  });
  return { macd: line, signal, histogram };
}

export interface OhlcLike {
  high: number;
  low: number;
  close: number;
}

export interface StochasticResult {
  k: (number | null)[];
  d: (number | null)[];
}

/**
 * Slow stochastic oscillator (KD). RSV = (C − LLn) / (HHn − LLn) · 100 over
 * `kPeriod` bars (50 when the window is flat), %K = SMA(kSmooth) of RSV,
 * %D = SMA(dSmooth) of %K. Defaults follow the common TW KD(9,3,3) setup.
 */
export function stochastic(
  bars: readonly OhlcLike[],
  kPeriod = 9,
  kSmooth = 3,
  dSmooth = 3
): StochasticResult {
  const rsv: (number | null)[] = new Array(bars.length).fill(null);
  for (let i = kPeriod - 1; i < bars.length; i++) {
    let hh = -Infinity;
    let ll = Infinity;
    for (let j = i - kPeriod + 1; j <= i; j++) {
      hh = Math.max(hh, bars[j].high);
      ll = Math.min(ll, bars[j].low);
    }
    rsv[i] = hh === ll ? 50 : ((bars[i].close - ll) / (hh - ll)) * 100;
  }
  const k = sma(rsv, kSmooth);
  const d = sma(k, dSmooth);
  return { k, d };
}
