import type { Signal } from '../types'
import { ActionChip, fmtPct, fmtPrice, fmtTime, Ring, actionTone } from './ui'

const STATUS_STYLE: Record<string, { bg: string; colour: string; label: string }> = {
  TRIGGERED: { bg: 'linear-gradient(180deg, #3ee3b1, #21b98a)', colour: '#04140f', label: 'SETUP CONFIRMED' },
  ARMED: { bg: 'linear-gradient(180deg, #ffcf6b, #f5b544)', colour: '#241a02', label: 'ARMED' },
  FORMING: { bg: 'rgba(56,189,248,0.16)', colour: '#8fb7ff', label: 'FORMING' },
  IDLE: { bg: 'rgba(255,255,255,0.04)', colour: 'var(--muted)', label: 'NO SETUP' },
  BLOCKED: { bg: 'rgba(245,181,68,0.14)', colour: 'var(--amber)', label: 'SETUP BLOCKED' },
}

export default function SignalPanel({ signal }: { signal: Signal }) {
  const { setup, plan, state, regime } = signal
  const tone = actionTone(signal.action)
  const style = STATUS_STYLE[setup.status] ?? STATUS_STYLE.IDLE
  const tdi = setup.tdi

  return (
    <section className={`panel signal-card ${tone === 'sell' ? 'sell' : ''}`}>
      <div className="signal-top">
        <div>
          <div className="hstack" style={{ gap: 10 }}>
            <span className="signal-sym">{signal.symbol.replace('USDT', '')}<span className="sym-quote">/USDT</span></span>
            <span className="pill-tag mono">{signal.timeframe}</span>
            <span className={`badge ${signal.source === 'demo' ? 'demo' : 'live'}`} style={{ padding: '2px 8px', fontSize: 10 }}>
              <span className={`dot ${signal.source === 'live' ? 'pulse' : ''}`} />
              {signal.source === 'live' ? 'Live' : signal.source === 'cache' ? 'Cached' : 'Demo'}
            </span>
          </div>
          <div className="signal-price">{fmtPrice(signal.price)}</div>
          <div className={`mono small ${signal.change_pct >= 0 ? 'pos' : 'neg'}`}>{fmtPct(signal.change_pct)} last bar</div>
        </div>

        <div className="signal-action">
          <span className="action-badge" style={{ background: style.bg, color: style.colour, borderColor: 'transparent' }}>
            {style.label}
          </span>
          <span className="tiny muted mono">{fmtTime(signal.bar_time)}</span>
        </div>
      </div>

      <div className="muted small" style={{ marginTop: 12 }}>{setup.text}</div>

      {/* Market context - the v2 filter: trade only inside structure, in a trend */}
      {regime && (
        <div className="plan-grid" style={{ marginTop: 12 }}>
          <div className="plan-box">
            <div className="k">Market</div>
            <div className="v" style={{ color: regime.regime === 'TRENDING' ? 'var(--green)' : 'var(--amber)' }}>
              {regime.regime === 'TRENDING' ? `TRENDING ${regime.trend}` : regime.regime}
            </div>
            <div className="tiny muted">{regime.reason}</div>
          </div>
          <div className="plan-box">
            <div className="k">Trend strength</div>
            <div className="v mono">{regime.adx != null ? `ADX ${regime.adx.toFixed(0)}` : '—'}</div>
            <div className="tiny muted">
              efficiency {regime.efficiency != null ? regime.efficiency.toFixed(2) : '—'} ·{' '}
              {regime.bars_since_bos != null ? `${regime.bars_since_bos.toFixed(0)} bars since break` : 'no break yet'}
            </div>
          </div>
          <div className="plan-box">
            <div className="k">Internal structure</div>
            <div className="v" style={{ color: regime.structure_ready ? 'var(--green)' : 'var(--muted)' }}>
              {regime.structure_ready ? 'READY' : 'NONE'}
            </div>
            <div className="tiny muted">
              {regime.structure_atr != null ? `${regime.structure_atr.toFixed(1)} ATR tall` : 'no minor swings yet'}
            </div>
          </div>
          <div className="plan-box">
            <div className="k">Liquidity</div>
            <div className="v" style={{ color: regime.liquidity_above || regime.liquidity_below ? 'var(--amber)' : 'var(--muted)' }}>
              {regime.liquidity_above && regime.liquidity_below
                ? 'BOTH SIDES'
                : regime.liquidity_above
                  ? 'POOL ABOVE'
                  : regime.liquidity_below
                    ? 'POOL BELOW'
                    : 'CLEAR'}
            </div>
            <div className="tiny muted">
              {regime.liquidity_above || regime.liquidity_below ? 'equal highs/lows - price usually sweeps them' : 'no clustered levels'}
            </div>
          </div>
        </div>
      )}

      <div className="ring-wrap">
        <Ring
          value={signal.confidence}
          label={signal.confidence_label}
          tone={setup.direction === 'SHORT' ? 'var(--red)' : 'var(--green)'}
        />
        <div style={{ flex: 1 }}>
          <div className="hstack" style={{ gap: 8 }}>
            {setup.direction && (
              <span className="chip-item" style={{ fontSize: 11.5 }}>
                <span className="muted">Side</span>
                <strong style={{ color: setup.direction === 'LONG' ? 'var(--green)' : 'var(--red)' }}>{setup.direction}</strong>
              </span>
            )}
            {setup.hvb_last_bars != null && (
              <span className="chip-item" style={{ fontSize: 11.5 }} title="Bars since the last High Volume Breakout on this chart">
                <span className="muted">Last HVB</span>
                <strong className="mono">
                  {setup.hvb_last_bars < 0 ? 'none yet' : setup.hvb_last_bars === 0 ? 'this bar' : `${setup.hvb_last_bars} bars ago`}
                </strong>
              </span>
            )}
            {setup.hvb_bars_ago != null && (
              <span className="chip-item" style={{ fontSize: 11.5 }}>
                <span className="muted">HVB</span>
                <strong className="mono">{setup.hvb_bars_ago === 0 ? 'this bar' : `${setup.hvb_bars_ago} bars ago`}</strong>
              </span>
            )}
            {state.position !== 'FLAT' && (
              <span className="chip-item" style={{ fontSize: 11.5 }}>
                <span className="muted">Open</span>
                <strong className={state.unrealized_pct != null && state.unrealized_pct >= 0 ? 'pos' : 'neg'}>
                  {fmtPct(state.unrealized_pct)} · {state.bars_in_state}b
                </strong>
              </span>
            )}
          </div>
          <div className="notes">
            {signal.confidence_notes.slice(0, 4).map((note) => (
              <span className="note-pill" key={note}>{note}</span>
            ))}
          </div>
        </div>
      </div>

      {/* Setup levels - the only "plan" this system needs */}
      <div style={{ marginTop: 18 }}>
        <div className="tiny muted" style={{ textTransform: 'uppercase', letterSpacing: '0.09em', fontWeight: 600, marginBottom: 8 }}>
          Structure
        </div>
        <div className="plan-grid">
          <div className="plan-box">
            <div className="k">Level to break</div>
            <div className="v">{fmtPrice(setup.level)}</div>
            <div className="tiny muted mono">{setup.level ? (setup.direction === 'LONG' ? 'swing high above' : 'swing low below') : 'no clean level'}</div>
          </div>
          <div className="plan-box">
            <div className="k">Distance</div>
            <div className="v">{setup.distance_pct != null ? fmtPct(setup.distance_pct) : '—'}</div>
            <div className="tiny muted">price → level</div>
          </div>
          {tdi && (
            <>
              <div className="plan-box">
                <div className="k">Purple (RSI)</div>
                <div className="v">{tdi.purple?.toFixed(2) ?? '—'}</div>
                <div className="tiny muted">{tdi.side ? `${tdi.side} the base line` : ''}</div>
              </div>
              <div className="plan-box">
                <div className="k">Middle band</div>
                <div className="v">{tdi.base?.toFixed(2) ?? '—'}</div>
                <div className="tiny muted">
                  {tdi.purple != null && tdi.base != null ? `gap ${Math.abs(tdi.purple - tdi.base).toFixed(2)} pts` : ''}
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      {/* Only strategies that actually use a stop show one */}
      {signal.strategy.uses_stops && plan?.entry != null && (
        <div style={{ marginTop: 16 }}>
          <div className="hstack" style={{ marginBottom: 8 }}>
            <span className="tiny muted" style={{ textTransform: 'uppercase', letterSpacing: '0.09em', fontWeight: 600 }}>
              Risk model · {plan.side}
            </span>
            <span className="pill-tag">{plan.is_live ? 'Live' : 'Illustrative'}</span>
          </div>
          <div className="plan-grid">
            <div className="plan-box">
              <div className="k">Stop</div>
              <div className="v neg">{fmtPrice(plan.stop)}</div>
            </div>
            {plan.take_profits.slice(0, 2).map((tp, i) => (
              <div className="plan-box" key={i}>
                <div className="k">Target {i + 1}</div>
                <div className="v pos">{fmtPrice(tp)}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ marginTop: 16 }}>
        <div className="tiny muted" style={{ textTransform: 'uppercase', letterSpacing: '0.09em', fontWeight: 600, marginBottom: 4 }}>
          Rule checks
        </div>
        <div className="rules">
          {signal.checks.map((check) => (
            <div className="rule" key={check.label}>
              <span className={`rule-icon ${check.passed ? 'pass' : 'fail'}`}>{check.passed ? '✓' : '·'}</span>
              <div>
                <div className="rule-label" style={{ color: check.passed ? 'var(--text)' : 'var(--muted)' }}>{check.label}</div>
                <div className="rule-detail">{check.detail}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="hstack" style={{ marginTop: 14, gap: 14 }}>
        <span className="tiny muted">Strategy · <strong style={{ color: 'var(--text)' }}>{signal.strategy.name}</strong></span>
        <span className="tiny muted">Position · <strong style={{ color: 'var(--text)' }}>{state.position}</strong></span>
        <ActionChip action={signal.action} />
      </div>
    </section>
  )
}
