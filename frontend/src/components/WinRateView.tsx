import { useState } from 'react'
import { api } from '../api'
import type { Meta, WinRateReport } from '../types'
import { ErrorBox, Field, Section, Spinner, StatTile, fmtPct, fmtTime } from './ui'

export default function WinRateView({ meta }: { meta: Meta }) {
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [days, setDays] = useState(30)
  const [strategy, setStrategy] = useState('tdi_shift')
  const [report, setReport] = useState<WinRateReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async (refresh = false) => {
    setLoading(true)
    setError(null)
    try {
      setReport(await api.winrate(symbol, days, strategy, refresh))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Win-rate study failed')
    } finally {
      setLoading(false)
    }
  }

  const s = report?.summary

  return (
    <div className="stack" style={{ gap: 16 }}>
      <Section title="Win-rate study">
        <div className="grid-4">
          <Field label="Instrument">
            <select className="select" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
              {meta.symbols.map((sym) => <option key={sym.symbol} value={sym.symbol}>{sym.symbol} · {sym.name}</option>)}
            </select>
          </Field>
          <Field label="Window (days)">
            <input className="input mono" type="number" min={1} max={120} value={days} onChange={(e) => setDays(Number(e.target.value))} />
          </Field>
          <Field label="Strategy">
            <select className="select" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              {meta.strategies.map((st) => <option key={st.id} value={st.id}>{st.name}</option>)}
            </select>
          </Field>
          <div className="field" style={{ justifyContent: 'flex-end' }}>
            <div className="hstack">
              <button className="btn primary" onClick={() => run(false)} disabled={loading}>
                {loading ? <><Spinner /> Running…</> : 'Run study'}
              </button>
              <button className="btn ghost" onClick={() => run(true)} disabled={loading} title="Ignore cached candles and download again">Redownload</button>
            </div>
          </div>
        </div>
        <div className="tiny muted" style={{ marginTop: 10 }}>
          Timeframes tested: {meta.scan_timeframes.join(' · ')} — no stop loss, no take profit; a trade opens on the
          setup and closes on the opposite TDI cross.
        </div>
      </Section>

      {error && <ErrorBox message={error} onRetry={() => run(false)} />}

      {!report && !loading && !error && (
        <div className="panel empty">Run the study to see the win rate for the last {days} days on {symbol}.</div>
      )}

      {report && (
        <>
          {!report.is_real_data && (
            <div className="error-box">
              <strong>Demo data.</strong> {report.caveat}
            </div>
          )}

          <div className="grid-4">
            <StatTile
              label="Overall win rate"
              value={s?.win_rate_pct != null ? `${s.win_rate_pct}%` : 'n/a'}
              sub={`${s?.wins ?? 0}W / ${s?.losses ?? 0}L of ${s?.trades ?? 0} trades`}
              tone={s?.win_rate_pct != null ? (s.win_rate_pct >= 50 ? 'pos' : 'neg') : undefined}
            />
            <StatTile label="Best timeframe" value={s?.best_timeframe ?? '—'} sub="highest win rate" />
            <StatTile label="Window" value={`${report.days} days`} sub={`tested ${s?.timeframes_tested ?? 0} timeframes`} />
            <StatTile label="Data source" value={report.sources.join(', ') || '—'} sub={report.is_real_data ? 'real exchange candles' : 'synthetic demo feed'} />
          </div>

          <Section
            title={`${report.symbol} · last ${report.days} days`}
            right={<span className="tiny muted">generated {fmtTime(report.generated_at)}</span>}
            bodyStyle={{ padding: 0 }}
          >
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>TF</th><th className="num">Bars</th><th className="num">Trades</th><th className="num">Wins</th>
                    <th className="num">Losses</th><th className="num">Win rate</th><th className="num">Return</th>
                    <th className="num">Buy &amp; hold</th><th className="num">Avg win</th><th className="num">Avg loss</th>
                    <th className="num">PF</th><th className="num">Max DD</th><th>Source</th>
                  </tr>
                </thead>
                <tbody>
                  {report.rows.map((row) => (
                    <tr key={row.timeframe}>
                      <td className="mono">{row.timeframe}</td>
                      {row.error ? (
                        <td colSpan={12} className="muted">error: {row.error}</td>
                      ) : (
                        <>
                          <td className="num">{row.bars?.toLocaleString()}</td>
                          <td className="num">{row.trades}</td>
                          <td className="num pos">{row.wins}</td>
                          <td className="num neg">{row.losses}</td>
                          <td className={`num ${(row.win_rate_pct ?? 0) >= 50 ? 'pos' : 'neg'}`}>
                            {row.win_rate_pct != null ? `${row.win_rate_pct}%` : '—'}
                          </td>
                          <td className={`num ${(row.total_return_pct ?? 0) >= 0 ? 'pos' : 'neg'}`}>{fmtPct(row.total_return_pct)}</td>
                          <td className="num muted">{fmtPct(row.buy_hold_return_pct)}</td>
                          <td className="num pos">{fmtPct(row.avg_win_pct)}</td>
                          <td className="num neg">{fmtPct(row.avg_loss_pct)}</td>
                          <td className="num">{row.profit_factor != null ? row.profit_factor.toFixed(2) : '—'}</td>
                          <td className="num neg">{fmtPct(row.max_drawdown_pct)}</td>
                          <td><span className={`chip ${row.source === 'demo' ? 'exit' : 'buy'}`}>{row.source}</span></td>
                        </>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <div className="tiny muted">
            Win rate alone is not enough: check the profit factor and average win/loss too. A 30% win rate can still be
            profitable with large winners, and a 70% win rate can lose money with one huge loser.
          </div>
        </>
      )}
    </div>
  )
}
