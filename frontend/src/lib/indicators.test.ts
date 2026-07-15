import { describe, it, expect } from "vitest";
import { sma, ema, bollinger, rsi, macd, stochastic } from "./indicators";

/** Assert numeric arrays with nulls, comparing numbers to 4 decimals. */
function expectSeries(actual: (number | null)[], expected: (number | null)[]) {
  expect(actual.length).toBe(expected.length);
  actual.forEach((v, i) => {
    const e = expected[i];
    if (e == null) expect(v).toBeNull();
    else expect(v).toBeCloseTo(e, 4);
  });
}

describe("sma", () => {
  it("computes the simple moving average with leading nulls", () => {
    expectSeries(sma([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
  });

  it("returns all nulls when the series is shorter than the period", () => {
    expectSeries(sma([1, 2], 3), [null, null]);
  });

  it("skips leading nulls in the input (composability)", () => {
    expectSeries(sma([null, null, 1, 2, 3], 2), [null, null, null, 1.5, 2.5]);
  });
});

describe("ema", () => {
  it("seeds with the SMA then applies k = 2/(period+1)", () => {
    // seed at idx2 = SMA3 = 2; k = 0.5 → 4*.5+2*.5 = 3; 5*.5+3*.5 = 4
    expectSeries(ema([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
  });

  it("matches a hand-computed EMA(2)", () => {
    // seed idx1 = 1.5; k = 2/3 → 3*⅔+1.5*⅓ = 2.5; 4*⅔+2.5*⅓ = 3.5; 4.5
    expectSeries(ema([1, 2, 3, 4, 5], 2), [null, 1.5, 2.5, 3.5, 4.5]);
  });

  it("returns all nulls when there is not enough data", () => {
    expectSeries(ema([1, 2], 5), [null, null]);
  });
});

describe("bollinger", () => {
  it("computes middle = SMA and bands at ±mult·population σ", () => {
    const { middle, upper, lower } = bollinger([1, 2, 3, 4, 5], 3, 2);
    // σ of any 3 consecutive integers = sqrt(2/3) ≈ 0.816497
    const sd = Math.sqrt(2 / 3);
    expectSeries(middle, [null, null, 2, 3, 4]);
    expectSeries(upper, [null, null, 2 + 2 * sd, 3 + 2 * sd, 4 + 2 * sd]);
    expectSeries(lower, [null, null, 2 - 2 * sd, 3 - 2 * sd, 4 - 2 * sd]);
  });

  it("collapses to the middle line when the window is flat", () => {
    const { middle, upper, lower } = bollinger([5, 5, 5], 3, 2);
    expectSeries(middle, [null, null, 5]);
    expectSeries(upper, [null, null, 5]);
    expectSeries(lower, [null, null, 5]);
  });
});

describe("rsi", () => {
  it("matches hand-computed Wilder RSI(2)", () => {
    // changes: +1 +1 −1 +1
    // idx2: avgGain=1, avgLoss=0 → 100
    // idx3: avgGain=(1·1+0)/2=.5, avgLoss=(0·1+1)/2=.5 → RS=1 → 50
    // idx4: avgGain=(.5·1+1)/2=.75, avgLoss=(.5·1+0)/2=.25 → RS=3 → 75
    expectSeries(rsi([1, 2, 3, 2, 3], 2), [null, null, 100, 50, 75]);
  });

  it("is 0 for a monotonically falling series", () => {
    const r = rsi([10, 9, 8, 7, 6], 2);
    expectSeries(r, [null, null, 0, 0, 0]);
  });

  it("returns all nulls when there are not enough changes", () => {
    expectSeries(rsi([1, 2, 3], 14), [null, null, null]);
  });

  it("is neutral for a completely flat window", () => {
    expectSeries(rsi([100, 100, 100, 100], 3), [null, null, null, 50]);
  });
});

describe("macd", () => {
  it("matches hand-computed MACD(2,3,2) on a linear series", () => {
    const { macd: line, signal, histogram } = macd([1, 2, 3, 4, 5], 2, 3, 2);
    // EMA2 = [·,1.5,2.5,3.5,4.5]; EMA3 = [·,·,2,3,4] → line = [·,·,.5,.5,.5]
    expectSeries(line, [null, null, 0.5, 0.5, 0.5]);
    // signal = EMA2 of the line (seeded on its first two values)
    expectSeries(signal, [null, null, null, 0.5, 0.5]);
    expectSeries(histogram, [null, null, null, 0, 0]);
  });

  it("equals EMA(fast) − EMA(slow) pointwise with default params", () => {
    const closes = Array.from({ length: 60 }, (_, i) => 100 + Math.sin(i / 5) * 10 + i * 0.3);
    const { macd: line } = macd(closes);
    const f = ema(closes, 12);
    const s = ema(closes, 26);
    line.forEach((v, i) => {
      if (s[i] == null) expect(v).toBeNull();
      else expect(v).toBeCloseTo((f[i] as number) - (s[i] as number), 10);
    });
  });
});

describe("stochastic", () => {
  const bar = (high: number, low: number, close: number) => ({ high, low, close });

  it("matches hand-computed KD(3,2,2)", () => {
    const bars = [
      bar(10, 8, 9),
      bar(11, 9, 10),
      bar(12, 10, 11),
      bar(11, 9, 10),
      bar(12, 10, 12),
    ];
    // RSV: idx2 = (11−8)/(12−8)·100 = 75; idx3 = (10−9)/(12−9)·100 = 33.3̅;
    //      idx4 = (12−9)/(12−9)·100 = 100
    // %K = SMA2(RSV): idx3 = 54.16̅; idx4 = 66.6̅
    // %D = SMA2(%K):  idx4 = 60.416̅
    const { k, d } = stochastic(bars, 3, 2, 2);
    expectSeries(k, [null, null, null, 54.1667, 66.6667]);
    expectSeries(d, [null, null, null, null, 60.4167]);
  });

  it("uses RSV = 50 when the window is flat", () => {
    const bars = [bar(5, 5, 5), bar(5, 5, 5), bar(5, 5, 5)];
    const { k } = stochastic(bars, 3, 1, 1);
    expectSeries(k, [null, null, 50]);
  });
});
