# Zone quality — measured on real BTC candles

> **Question this answers:** *"ye ajeeb ajeeb algo candles … un pe stop loss hit ho
> gaya"* — which of the zones the scanner draws are actually worth trading, and
> can that be shown with numbers instead of opinion?
>
> **Answer, short:** on 90 days of real 1m BTC, the grade the scanner now prints
> separates the zones that travel in your favour from the ones that do not —
> A+ / A reach 2×ATR in 83% of cases, C only 54%. Block thickness is the single
> biggest factor. Nothing about *detection* was changed to get this.

## How it is measured

```bash
cd smc-radar
python tools/zone_quality.py --csv study-data/BTCUSDT-1m-90d.csv --timeframe 1m --horizon 240
```

* Data: real Binance/BTCUSDT 1m klines, **1 Jan – 31 Mar 2025**, 129,600 candles
  (see [`data.md`](data.md)).
* The map is replayed candle by candle; every zone it produced is kept.
* **Start of the trade:** the bar where price first comes back into the block —
  the map records that moment itself (`mitigated_index` / `mitigated_price`), so
  nothing looks into the future.
* **Failure:** a candle **closes** beyond the exact high/low the zone is about
  (plus a 0.05 ATR pad). A wick through it is not a stop.
* **MFE:** how far price then travelled in the trade's direction, in ATR,
  before that failure. Quoted at 1, 2 and 3 ATR so every group is judged at the
  same distance — no invented R ladder, no favourite target.

## Result: the grade

| grade | zones | failed | reached 1 ATR | 2 ATR | 3 ATR | avg MFE |
|---|---|---|---|---|---|---|
| **A+** | 561 | 81% | 95% | **83%** | 70% | 9.34 ATR |
| **A** | 8,389 | 84% | 96% | **83%** | 67% | 8.15 ATR |
| **B** | 5,269 | 88% | 87% | 69% | 53% | 6.92 ATR |
| **C** | 712 | 90% | 76% | **55%** | 41% | 5.31 ATR |

(14,931 zones in total; the run before the order-block geometry was corrected
gave the same ordering — 551 / 8,267 / 5,105 / 805 with 83 / 83 / 68 / 54 %.)

Read it as: a C zone has a 1-in-2 chance of giving you 2 ATR before the level is
closed through; an A zone 5-in-6. A+ and A are the same on this measure — A+ is
kept because its average travel is larger (9.3 vs 8.2 ATR).

## Result: what actually moves the number

| feature | best | worst |
|---|---|---|
| **block thickness** | ≥1 ATR: **78%** reach 2 ATR | <0.25 ATR: **36%** |
| displacement leg | ≥4 ATR: **83%** | <1 ATR: 42% |
| FVG size | ≥1.5 ATR: 80% | <0.4 ATR: 72% |
| pattern kind | rejection block ~88%, very strong AC ~83% | plain strong high/low ~70% |
| swept level was a major one | 80% | 76% |
| a strong level at the same price | 74% | 77% (no lift) |
| trend at the moment of the touch | 77% | 76% (no lift) |

That is why the grade weighs block thickness, displacement, FVG size and the
pattern kind, and only lightly weighs confluence and the trend.

## What this study does **not** say

* It is **BTC 1m only**. Other pairs and timeframes will differ; re-run the
  command with another CSV before trusting the same weights there.
* The trend-at-touch comparison used a **fixed** 1 ATR target and showed no
  difference. In an earlier version of this study, with the target set to the
  next liquidity pool, trend-aligned zones won 62% vs 35% — the alignment
  changes *how far* price travels, not the odds of the first 2 ATR. The
  "tradeable now" badge is the book's HTF-direction rule; it is not claimed as a
  measured edge.
* 85% of all zones see a close beyond their level within 4 hours. These are
  short-lived levels: the stop belongs just beyond the level, and the trade is
  taken soon after the touch, not held all day.
* Numbers move with the horizon and the data. Re-run the command; the tool
  prints the file, the timeframe and the candle count in its first line.

## How the grade is built (traceable)

`bot/map.py::MarketMap.score_zone` — every zone carries `score` (0–100), `grade`,
`trend`, `confluence`, `htf`, `block_atr`, `disp_atr`, `fvg_atr`, `swept_major`
and the `ob` candle it was drawn on, so any chip on the page can be traced back
to the candle that produced it.
