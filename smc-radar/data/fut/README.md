# Futures replay folder

`run.py replay data` reads every CSV under `data/`, so this folder is where a
**futures** recording goes: one file per contract, named `<SYMBOL>-....csv` with
MEXC's own contract names (`BTC_USDT-1m-2025-01.csv`), columns
`open_time, open, high, low, close, volume`.

Live futures data is real: `run.py serve` opens MEXC's contract API
(`contract.mexc.com`, `BTC_USDT` naming, its own kline endpoint and its own
websocket), so a contract's candles are contract candles — not spot candles with
a different label.

The file in this folder is **not** futures market data: it is the real recorded
*spot* BTC candles copied under a contract name, and it exists only so the
futures code path (feed, engine, two-section page) can be exercised in replay,
where MEXC is unreachable. Replay is labelled `▶ replay` everywhere, and the
page never claims it is live. Delete it before recording your own contracts.
