import type { Signal } from '../types'
import { ActionChip, fmtPct, fmtPrice, fmtTime, Ring, actionTone } from './ui'

export default function SignalPanel({ signal }: { signal: Signal }) {
  const { plan, state } = signal
  const tone = actionTone(signal.action)
  const direction = plan.side
  const isLong = direction === 'LONG'
  const price = signal.price

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
          <div className="signal-price">{fmtPrice(price)}</div>
          <div className={`mono small ${signal.change_pct >= 0 ? 'pos' : 'neg'}`}>{fmtPct(signal.change_pct)} last bar</div>
        </div>

        <div className="signal-action">
          <span className={`action-badge ${tone}`}>{signal.action}</span>
          <span className="tiny muted mono">{fmtTime(signal.bar_time)}</span>
        </div>
      </div>

      <div className="muted small" style={{ marginTop: 10 }}>{signal.action_reason}</div>

      <div className="ring-wrap">
        <Ring value={signal.confidence} label={signal.confidence_label} tone={signal.bias === 'SHORT' ? 'var(--red)' : 'var(--green)'} />
        <div style={{ flex: 1 }}>
          <div className="hstack" style={{ gap: 8 }}>
            <span className="chip-item" style={{ fontSize: 11.5 }}>
              <span className="muted">Bias</span>
              <strong style={{ color: signal.bias === 'LONG' ? 'var(--green)' : signal.bias === 'SHORT' ? 'var(--red)' : 'var(--muted)' }}>
                {signal.bias}
              </strong>
            </span>
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

      <div style={{ marginTop: 18 }}>
        <div className="hstack" style={{ marginBottom: 8 }}>
          <span className="tiny muted" style={{ textTransform: 'uppercase', letterSpacing: '0.09em', fontWeight: 600 }}>
            Trade plan · {direction}
          </span>
          <span className="pill-tag">{plan.is_live ? 'Live' : 'Illustrative'}</span>
        </div>
        <div className="plan-grid">
          <div className="plan-box">
            <div className="k">Entry</div>
            <div className="v">{fmtPrice(plan.entry)}</div>
          </div>
          <div className="plan-box">
            <div className="k">Stop loss</div>
            <div className="v neg">{fmtPrice(plan.stop)}</div>
            <div className="tiny muted mono">{plan.risk_pct != null ? `${plan.risk_pct.toFixed(2)}% risk` : ''}</div>
          </div>
          {plan.take_profits.slice(0, 3).map((tp, i) => (
            <div className="plan-box" key={i}>
              <div className="k">Target {i + 1} {plan.risk_reward[i] != null ? `· ${plan.risk_reward[i]}R` : ''}</div>
              <div className="v pos">{fmtPrice(tp)}</div>
            </div>
          ))}
        </div>
        {!plan.is_live && <div className="tiny muted" style={{ marginTop: 8 }}>{plan.note}</div>}

        {signal.last_trigger && (
          <div className="info-box" style={{ marginTop: 12 }}>
            Last {signal.last_trigger.side.toLowerCase()} trigger {signal.last_trigger.bars_ago === 0 ? 'on the latest bar' : `${signal.last_trigger.bars_ago} bars ago`} at{' '}
            <span className="mono">{fmtPrice(signal.last_trigger.price)}</span> · {fmtTime(signal.last_trigger.time)}
          </div>
        )}
      </div>

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
        {state.entry_price != null && (
          <span className="tiny muted">Avg entry · <strong className="mono" style={{ color: 'var(--text)' }}>{fmtPrice(state.entry_price)}</strong></span>
        )}
      </div>
    </section>
  )
}
