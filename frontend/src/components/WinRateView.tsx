import { useState } from 'react'
import { api } from '../api'
import type { DataSourceInfo, Meta, WinRateReport } from '../types'
import { ErrorBox, Field, Section, Spinner, StatTile, fmtPct, fmtTime } from './ui'

export default function WinRateView({ meta }: { meta: Meta }) {
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [days, setDays] = useState(30)
  const [strategy, setStrategy] = useState('tdi_shift')
  const [report, setReport] = useState<WinRateReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [trail, setTrail] = useState<DataSourceInfo | null>(null)
  const [trailLoading, setTrailLoading] = useState(false)
  const [trailError, setTrailError] = useState<string | null>(null)
  const [showTrail, setShowTrail] = useState(false)

  // Ask the backend, step by step, where the candles would come from.
  const checkData = async () => {
    setShowTrail(true)
    setTrailLoading(true)
    setTrailError(null)
    try {
      setTrail(await api.dataSource(symbol, meta.scan_timeframes[0] ?? '15m', days))
    } catch (err) {
      setTrailError(err instanceof Error ? err.message : 'Data check failed')
    } finally {
      setTrailLoading(false)
    }
  }

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
              {meta.strategies.map((st) => (
                <option key={st.id} value={st.id}>{st.name} · v{st.version}</option>
              ))}
            </select>
          </Field>
          <div className="field" style={{ justifyContent: 'flex-end' }}>
            <div className="hstack">
              <button className="btn primary" onClick={() => run(false)} disabled={loading}>
                {loading ? <><Spinner /> Running…</> : 'Run study'}
              </button>
              <button className="btn ghost" onClick={() => run(true)} disabled={loading} title="Ignore cached candles and download again">Redownload</button>
              <button className="btn ghost" onClick={checkData} disabled={trailLoading} title="Show where the candles come from">
                {trailLoading ? <><Spinner /> Checking…</> : 'Check data'}
              </button>
            </div>
          </div>
        </div>
        <div className="tiny muted" style={{ marginTop: 10 }}>
          Timeframes tested: {meta.scan_timeframes.join(' · ')} — no stop loss, no take profit; a trade opens on the
          setup and closes on the opposite TDI cross.
        </div>
      </Section>

      {showTrail && (
        <Section
          title="Where the candles come from"
          right={
            <button className="btn ghost sm" onClick={() => setShowTrail(false)}>
              Hide
            </button>
          }
        >
          {trailError && <div className="error-box">{trailError}</div>}
          {trailLoading && !trail && <div className="tiny muted"><Spinner /> asking the exchange…</div>}
          {trail && (
            <div className="stack" style={{ gap: 12 }}>
              <div className="tiny muted">
                Window {trail.window.start} → {trail.window.end} · about {trail.bars_expected.toLocaleString()} candles
                of {trail.bar_seconds}s
              </div>
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Exchange host</th><th>Reachable</th><th className="num">Latency</th><th>Answer</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trail.hosts.map((host) => (
                      <tr key={host.host}>
                        <td className="mono tiny">{host.host}</td>
                        <td>
                          <span className={`chip ${host.ok ? 'buy' : 'exit'}`}>{host.ok ? 'yes' : 'no'}</span>
                        </td>
                        <td className="num">{host.ms} ms</td>
                        <td className="tiny muted">{host.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="tiny muted mono">
                cache: {trail.cache.path}
                {trail.cache.exists
                  ? ` · ${trail.cache.bars?.toLocaleString()} bars, downloaded ${trail.cache.fetched_at}`
                  : ' · empty (nothing downloaded yet)'}
              </div>
              <div className="tiny">
                Using <strong>{trail.bars.toLocaleString()}</strong> candles {trail.first_candle} → {trail.last_candle},
                close {trail.first_close.toLocaleString()} → {trail.last_close.toLocaleString()} · source{' '}
                <span className={`chip ${trail.is_real ? 'buy' : 'exit'}`}>{trail.source}</span>
                {!trail.is_real && ' — synthetic, not market data'}
              </div>
            </div>
          )}
        </Section>
      )}

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

          {s?.low_sample && (
            <div className="error-box">
              <strong>Too few trades.</strong> Only {s.trades} setups were taken in this window, so the win rate above
              is not statistically meaningful. Use a longer window or a lower-threshold version.
            </div>
          )}

          {s?.raw_setups != null && s.raw_setups > 0 && (
            <div className="tiny muted">
              Structure filter: <strong>{s.raw_setups}</strong> raw setups (previous version) →{' '}
              <strong>{s.taken_setups}</strong> taken by this version. The rest were blocked as ranging,
              against-trend, outside structure or into liquidity.
            </div>
          )}

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
                    <th className="num">PF</th><th className="num">Max DD</th><th className="num">Setups</th><th>Source</th>
                  </tr>
                </thead>
                <tbody>
                  {report.rows.map((row) => (
                    <tr key={row.timeframe}>
                      <td className="mono">{row.timeframe}</td>
                      {row.error ? (
                        <td colSpan={13} className="muted">error: {row.error}</td>
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
                          <td className="num tiny">
                            {row.filters ? `${row.filters.taken} / ${row.filters.raw}` : `${row.trades ?? 0}`}
                          </td>
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
