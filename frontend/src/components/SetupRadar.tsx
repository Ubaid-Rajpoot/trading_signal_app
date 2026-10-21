import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { Meta, RadarCell, RadarResponse } from '../types'
import { ErrorBox, Section, Spinner, fmtAgo, fmtPct, fmtPrice } from './ui'

const STATUS_META: Record<string, { label: string; colour: string; bg: string; order: number }> = {
  TRIGGERED: { label: 'SETUP', colour: '#04140f', bg: '#2fd6a3', order: 0 },
  ARMED: { label: 'ARMED', colour: '#241a02', bg: '#f5b544', order: 1 },
  FORMING: { label: 'FORMING', colour: '#8fb7ff', bg: 'rgba(56,189,248,0.16)', order: 2 },
  BLOCKED: { label: 'BLOCKED', colour: 'var(--amber)', bg: 'rgba(245,181,68,0.10)', order: 3 },
  IDLE: { label: '—', colour: 'var(--muted-2)', bg: 'rgba(255,255,255,0.03)', order: 4 },
  ERROR: { label: 'ERR', colour: 'var(--red)', bg: 'var(--red-soft)', order: 5 },
}

interface Props {
  meta: Meta
  watchlist: string[]
  strategyId: string
  onOpen: (symbol: string, timeframe: string) => void
}

export default function SetupRadar({ meta, watchlist, strategyId, onOpen }: Props) {
  const [data, setData] = useState<RadarResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const timeframes = meta.scan_timeframes

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await api.radar(watchlist, timeframes, strategyId))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Radar scan failed')
    } finally {
      setLoading(false)
    }
  }, [watchlist.join(','), timeframes.join(','), strategyId])

  useEffect(() => {
    load()
    const timer = setInterval(load, 60_000)
    return () => clearInterval(timer)
  }, [load])

  const cellFor = (cells: Record<string, RadarCell>, tf: string): RadarCell => cells[tf] ?? { status: 'IDLE' }

  return (
    <div className="stack" style={{ gap: 16 }}>
      <Section
        title="Setup radar"
        right={
          <div className="hstack" style={{ gap: 8 }}>
            <span className="tiny muted">{data ? `scanned ${fmtAgo(data.generated_at)}` : ''}</span>
            <button className="btn sm ghost" onClick={load}>{loading ? <Spinner /> : 'Rescan'}</button>
          </div>
        }
      >
        <div className="tiny muted" style={{ marginBottom: 12 }}>
          Har cell batata hai ke us instrument + timeframe par abhi kya ho raha hai. <strong>BLOCKED</strong> ka
          matlab: setup ban raha hai lekin rules mana kar rahe hain (market range me hai, trend ke khilaf hai, ya
          liquidity build-up saamne hai). Kisi bhi cell par click karke chart khol sakte hain.
        </div>
        {data && data.blocked_count > 0 && (
          <div className="tiny" style={{ marginBottom: 8, color: 'var(--amber)' }}>
            {data.blocked_count} setup{data.blocked_count === 1 ? '' : 's'} blocked by the structure filter right now —
            v1 would have traded them.
          </div>
        )}

        {error && <ErrorBox message={error} onRetry={load} />}

        <div className="table-wrap">
          <table className="data radar-table">
            <thead>
              <tr>
                <th>Instrument</th>
                {timeframes.map((tf) => (
                  <th key={tf} className="num">{tf}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(data?.grid ?? watchlist.map((symbol) => ({ symbol, cells: {} }))).map((row) => (
                <tr key={row.symbol}>
                  <td className="mono" style={{ fontWeight: 600 }}>{row.symbol.replace('USDT', '')}<span className="sym-quote">/USDT</span></td>
                  {timeframes.map((tf) => {
                    const cell = cellFor(row.cells, tf)
                    const meta = STATUS_META[cell.status] ?? STATUS_META.IDLE
                    const clickable = cell.status !== 'IDLE' && cell.status !== 'ERROR'
                    return (
                      <td key={tf} className="num" style={{ padding: 4 }}>
                        <button
                          className="radar-cell"
                          style={{ background: meta.bg, color: meta.colour, opacity: cell.status === 'BLOCKED' ? 0.75 : 1 }}
                          disabled={!clickable}
                          onClick={() => onOpen(row.symbol, tf)}
                          title={cell.text || cell.error || `${row.symbol} ${tf}`}
                        >
                          <span className="radar-status">{meta.label}</span>
                          {cell.direction && <span className="radar-dir">{cell.direction === 'LONG' ? '▲' : '▼'}</span>}
                          {cell.distance_pct != null && (
                            <span className="radar-dist">{fmtPct(cell.distance_pct, 2)}</span>
                          )}
                        </button>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="hstack" style={{ marginTop: 12, gap: 14 }}>
          {Object.entries(STATUS_META).filter(([k]) => k !== 'ERROR').map(([key, m]) => (
            <span key={key} className="legend-item">
              <span className="radar-legend" style={{ background: m.bg, border: `1px solid ${key === 'IDLE' ? 'var(--border)' : 'transparent'}` }} />
              {m.label === '—' ? 'no setup' : m.label.toLowerCase()}
            </span>
          ))}
        </div>
      </Section>

      <Section title={`Actionable now (${data?.actionable.length ?? 0})`} bodyStyle={{ padding: 0 }}>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Instrument</th><th>TF</th><th>Status</th><th>Side</th><th className="num">Price</th>
                <th className="num">Level to break</th><th className="num">Distance</th><th>What's happening</th>
              </tr>
            </thead>
            <tbody>
              {(data?.actionable ?? []).map((row) => {
                const m = STATUS_META[row.status] ?? STATUS_META.IDLE
                return (
                  <tr key={`${row.symbol}-${row.timeframe}`} style={{ cursor: 'pointer' }} onClick={() => onOpen(row.symbol, row.timeframe)}>
                    <td className="mono">{row.symbol.replace('USDT', '')}/USDT</td>
                    <td className="mono muted">{row.timeframe}</td>
                    <td><span className="radar-legend" style={{ background: m.bg, display: 'inline-block', marginRight: 6 }} />{m.label}</td>
                    <td style={{ color: row.direction === 'LONG' ? 'var(--green)' : row.direction === 'SHORT' ? 'var(--red)' : 'var(--muted)' }}>
                      {row.direction ?? '—'}
                    </td>
                    <td className="num">{fmtPrice(row.price)}</td>
                    <td className="num">{fmtPrice(row.level)}</td>
                    <td className="num">{fmtPct(row.distance_pct, 2)}</td>
                    <td className="muted" style={{ whiteSpace: 'normal', maxWidth: 380 }}>{row.text}</td>
                  </tr>
                )
              })}
              {!data?.actionable.length && (
                <tr><td colSpan={8}><div className="empty">Abhi kisi instrument par setup nahi ban raha. Radar har minute khud refresh hota hai.</div></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  )
}
