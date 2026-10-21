import { useMemo, useState } from 'react'
import { api } from '../api'
import type { BacktestResult, Meta } from '../types'
import EquityChart from './EquityChart'
import ParamsEditor from './ParamsEditor'
import { ErrorBox, Field, Section, Spinner, StatTile, fmtPct, fmtPrice, fmtTime } from './ui'

interface Props {
  meta: Meta
  initialSymbol: string
  initialTimeframe: string
  initialStrategy: string
}

export default function BacktestView({ meta, initialSymbol, initialTimeframe, initialStrategy }: Props) {
  const [symbol, setSymbol] = useState(initialSymbol)
  const [timeframe, setTimeframe] = useState(initialTimeframe)
  const [strategyId, setStrategyId] = useState(initialStrategy)
  const [bars, setBars] = useState(800)
  const [risk, setRisk] = useState(1)
  const [equity, setEquity] = useState(10000)
  const [fees, setFees] = useState(5)
  const [slippage, setSlippage] = useState(2)
  const [leverage, setLeverage] = useState(1)
  const [showParams, setShowParams] = useState(false)
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [sideFilter, setSideFilter] = useState<'ALL' | 'LONG' | 'SHORT'>('ALL')

  const schema = useMemo(() => meta.strategies.find((s) => s.id === strategyId) ?? meta.strategies[0], [meta, strategyId])
  const [params, setParams] = useState<Record<string, number | boolean | string>>({ ...schema.defaults })

  const run = async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.backtest({
        symbol,
        timeframe,
        strategy: strategyId,
        params,
        bars,
        initial_equity: equity,
        risk_pct: risk,
        fee_bps: fees,
        slippage_bps: slippage,
        leverage,
      })
      setResult(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Backtest failed')
      setResult(null)
    } finally {
      setLoading(false)
    }
  }

  const trades = result?.trades ?? []
  const filtered = sideFilter === 'ALL' ? trades : trades.filter((t) => t.side === sideFilter)
  const m = result?.metrics

  return (
    <div className="stack" style={{ gap: 16 }}>
      <Section
        title="Backtest configuration"
        right={<button className="btn sm ghost" onClick={() => setShowParams((v) => !v)}>{showParams ? 'Hide parameters' : 'Strategy parameters'}</button>}
      >
        <div className="grid-4">
          <Field label="Symbol">
            <select className="select" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
              {meta.symbols.map((s) => <option key={s.symbol} value={s.symbol}>{s.symbol} · {s.name}</option>)}
            </select>
          </Field>
          <Field label="Timeframe">
            <select className="select" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
              {meta.timeframes.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </Field>
          <Field label="Strategy">
            <select
              className="select"
              value={strategyId}
              onChange={(e) => {
                const next = meta.strategies.find((s) => s.id === e.target.value)!
                setStrategyId(next.id)
                setParams({ ...next.defaults })
              }}
            >
              {meta.strategies.map((s) => (
                <option key={s.id} value={s.id}>{s.name} · v{s.version}</option>
              ))}
            </select>
          </Field>
          <Field label="History (bars)" hint="Max 1000 per exchange request">
            <input className="input mono" type="number" min={100} max={1000} step={50} value={bars} onChange={(e) => setBars(Number(e.target.value))} />
          </Field>
          <Field label="Starting equity ($)">
            <input className="input mono" type="number" min={100} step={100} value={equity} onChange={(e) => setEquity(Number(e.target.value))} />
          </Field>
          <Field label="Risk per trade (%)">
            <input className="input mono" type="number" min={0.1} max={100} step={0.1} value={risk} onChange={(e) => setRisk(Number(e.target.value))} />
          </Field>
          <Field label="Fees (bps per side)">
            <input className="input mono" type="number" min={0} step={0.5} value={fees} onChange={(e) => setFees(Number(e.target.value))} />
          </Field>
          <Field label="Slippage (bps per side)">
            <input className="input mono" type="number" min={0} step={0.5} value={slippage} onChange={(e) => setSlippage(Number(e.target.value))} />
          </Field>
          <Field label="Max leverage">
            <input className="input mono" type="number" min={1} max={20} step={0.5} value={leverage} onChange={(e) => setLeverage(Number(e.target.value))} />
          </Field>
          <div className="field" style={{ justifyContent: 'flex-end' }}>
            <button className="btn primary" onClick={run} disabled={loading}>
              {loading ? <><Spinner /> Running…</> : 'Run backtest'}
            </button>
          </div>
        </div>
        {showParams && (
          <div style={{ marginTop: 16, paddingTop: 14, borderTop: '1px dashed var(--border)' }}>
            <ParamsEditor schema={schema} values={params} onChange={setParams} />
          </div>
        )}
      </Section>

      {error && <ErrorBox message={error} onRetry={run} />}

      {!result && !loading && !error && (
        <div className="panel empty">
          Configure the run above and hit <strong>Run backtest</strong>. Results include fees, slippage and full trade history.
        </div>
      )}

      {m && result && (
        <>
          <div className="grid-4">
            <StatTile
              label="Total return"
              value={fmtPct(m.total_return_pct)}
              sub={`Buy & hold ${fmtPct(m.buy_hold_return_pct)}`}
              tone={m.total_return_pct >= 0 ? 'pos' : 'neg'}
            />
            <StatTile label="Max drawdown" value={fmtPct(m.max_drawdown_pct)} sub={`Exposure ${m.exposure_pct}%`} tone="neg" />
            <StatTile label="Win rate" value={`${m.win_rate_pct}%`} sub={`${m.wins}W / ${m.losses}L of ${m.trades}`} />
            <StatTile label="Profit factor" value={m.profit_factor == null ? '∞' : m.profit_factor.toFixed(2)} sub={`Expectancy ${m.expectancy_r}R`} />
          </div>

          <Section
            title="Equity curve"
            right={<span className="badge"><span className="dot" />{result.source === 'live' ? 'Live data' : result.source === 'cache' ? 'Cached data' : 'Demo data'}</span>}
          >
            <EquityChart result={result} />
          </Section>

          <div className="grid-2">
            <Section title="Performance detail">
              <div className="stack" style={{ gap: 0 }}>
                {[
                  ['Initial equity', `$${m.initial_equity.toLocaleString()}`],
                  ['Final equity', `$${m.final_equity.toLocaleString()}`],
                  ['CAGR', fmtPct(m.cagr_pct)],
                  ['Sharpe', m.sharpe.toFixed(2)],
                  ['Sortino', m.sortino.toFixed(2)],
                  ['Avg win', fmtPct(m.avg_win_pct)],
                  ['Avg loss', fmtPct(m.avg_loss_pct)],
                  ['Best trade', fmtPct(m.best_trade_pct)],
                  ['Worst trade', fmtPct(m.worst_trade_pct)],
                  ['Avg holding', `${m.avg_bars_held} bars`],
                  ['Bars tested', m.bars.toLocaleString()],
                ].map(([label, value]) => (
                  <div className="row-between" key={label} style={{ padding: '7px 0', borderBottom: '1px dashed var(--border-soft)' }}>
                    <span className="small muted">{label}</span>
                    <span className="mono small">{value}</span>
                  </div>
                ))}
              </div>
            </Section>

            <Section title="Assumptions">
              <div className="stack small muted" style={{ gap: 8 }}>
                <div className="info-box">
                  Fills happen at the <strong>next bar open</strong> after a trigger closes. Stops and targets are checked
                  intrabar against high/low; when both are touched in one bar, the stop is assumed to fill first.
                </div>
                <div className="row-between"><span>Fees</span><span className="mono">{fees} bps / side</span></div>
                <div className="row-between"><span>Slippage</span><span className="mono">{slippage} bps / side</span></div>
                <div className="row-between"><span>Risk sizing</span><span className="mono">{risk}% of equity per trade</span></div>
                <div className="row-between"><span>Max leverage</span><span className="mono">{leverage}×</span></div>
                <div className="row-between"><span>Strategy</span><span className="mono">{result.strategy.name}</span></div>
                <div className="row-between"><span>Window</span><span className="mono">{fmtTime(m.start)} → {fmtTime(m.end)}</span></div>
              </div>
            </Section>
          </div>

          <Section
            title={`Trades (${filtered.length})`}
            right={
              <div className="segmented">
                {(['ALL', 'LONG', 'SHORT'] as const).map((side) => (
                  <button key={side} className={sideFilter === side ? 'active' : ''} onClick={() => setSideFilter(side)}>{side}</button>
                ))}
              </div>
            }
            bodyStyle={{ padding: 0 }}
          >
            <div className="table-wrap" style={{ maxHeight: 460, overflowY: 'auto' }}>
              <table className="data">
                <thead>
                  <tr>
                    <th>#</th><th>Side</th><th>Entry</th><th>Exit</th><th>Entry px</th><th>Exit px</th>
                    <th className="num">R</th><th className="num">PnL</th><th className="num">PnL %</th><th>Held</th><th>Exit reason</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((t, i) => (
                    <tr key={`${t.entry_time}-${i}`}>
                      <td className="mono muted">{i + 1}</td>
                      <td><span className={`chip ${t.side === 'LONG' ? 'buy' : 'sell'}`}>{t.side}</span></td>
                      <td className="mono">{fmtTime(t.entry_time)}</td>
                      <td className="mono">{fmtTime(t.exit_time)}</td>
                      <td className="mono">{fmtPrice(t.entry_price)}</td>
                      <td className="mono">{fmtPrice(t.exit_price)}</td>
                      <td className={`num ${t.r_multiple >= 0 ? 'pos' : 'neg'}`}>{t.r_multiple.toFixed(2)}</td>
                      <td className={`num ${t.pnl >= 0 ? 'pos' : 'neg'}`}>{t.pnl.toFixed(2)}</td>
                      <td className={`num ${t.pnl_pct >= 0 ? 'pos' : 'neg'}`}>{fmtPct(t.pnl_pct)}</td>
                      <td className="num">{t.bars_held}</td>
                      <td><span className="pill-tag">{t.exit_reason}</span></td>
                    </tr>
                  ))}
                  {filtered.length === 0 && (
                    <tr><td colSpan={11}><div className="empty">No trades matched this filter.</div></td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </Section>
        </>
      )}
    </div>
  )
}
