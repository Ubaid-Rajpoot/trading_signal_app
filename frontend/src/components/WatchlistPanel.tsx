import type { ScanRow } from '../types'
import { ActionChip, Spinner, fmtPct, fmtPrice } from './ui'

interface Props {
  rows: ScanRow[]
  selected: string
  loading: boolean
  onSelect: (symbol: string) => void
}

export default function WatchlistPanel({ rows, selected, loading, onSelect }: Props) {
  return (
    <section className="panel" style={{ overflow: 'hidden' }}>
      <header className="panel-head">
        <span className="panel-title">Watchlist</span>
        <span className="right">
          {loading ? <Spinner /> : <span className="tiny muted mono">{rows.length}</span>}
        </span>
      </header>
      <div className="watchlist">
        {rows.length === 0 && <div className="empty">Scanning the market…</div>}
        {rows.map((row) => {
          const base = row.symbol.replace('USDT', '')
          const active = row.symbol === selected
          return (
            <div
              key={row.symbol}
              className={`watch-row ${active ? 'selected' : ''}`}
              onClick={() => onSelect(row.symbol)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => e.key === 'Enter' && onSelect(row.symbol)}
            >
              <div>
                <div className="watch-sym">
                  <span className="sym">
                    <span className="sym-base">{base}</span>
                    <span className="sym-quote">/USDT</span>
                  </span>
                  {row.error && <span className="pill-tag" style={{ color: 'var(--red)' }}>error</span>}
                </div>
                <div className="watch-meta">
                  {!row.error && row.action && <ActionChip action={row.action} />}
                  {!row.error && row.confidence != null && (
                    <span className="confbar" title={`${row.confidence}%`}>
                      <i style={{ width: `${row.confidence}%` }} />
                    </span>
                  )}
                  {row.unrealized_pct != null && Math.abs(row.unrealized_pct) >= 0.05 && (
                    <span className={`tiny mono ${row.unrealized_pct >= 0 ? 'pos' : 'neg'}`}>{fmtPct(row.unrealized_pct, 1)}</span>
                  )}
                </div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div className="watch-price">{row.error ? '—' : fmtPrice(row.price)}</div>
                <div className={`tiny mono ${(row.change_pct ?? 0) >= 0 ? 'pos' : 'neg'}`}>
                  {row.error ? row.error.slice(0, 22) : fmtPct(row.change_pct, 2)}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}
