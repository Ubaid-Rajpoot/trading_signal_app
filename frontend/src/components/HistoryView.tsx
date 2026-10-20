import { useEffect, useState } from 'react'
import { api } from '../api'
import type { NotificationRow, SchedulerStatus, StoredSignal } from '../types'
import { ActionChip, ErrorBox, Section, Spinner, StatTile, fmtAgo, fmtPrice, fmtTime } from './ui'

export default function HistoryView() {
  const [signals, setSignals] = useState<StoredSignal[]>([])
  const [notifications, setNotifications] = useState<NotificationRow[]>([])
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const [history, alerts] = await Promise.all([api.history(200), api.alerts()])
      setSignals(history.items)
      setNotifications(alerts.items)
      setScheduler(alerts.scheduler)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load history')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 60_000)
    return () => clearInterval(timer)
  }, [])

  const actionable = signals.filter((s) => s.action === 'BUY' || s.action === 'SELL')

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid-4">
        <StatTile label="Stored signals" value={String(signals.length)} sub="Deduplicated per bar" />
        <StatTile label="Actionable" value={String(actionable.length)} sub="BUY / SELL triggers" />
        <StatTile label="Scanner runs" value={String(scheduler?.scans ?? 0)} sub={scheduler?.running ? 'Background scanner active' : 'Scanner stopped'} />
        <StatTile
          label="Alerts delivered"
          value={String(scheduler?.alerts_sent ?? 0)}
          sub={scheduler?.last_run ? `Last scan ${fmtAgo(scheduler.last_run)}` : 'No scan yet'}
        />
      </div>

      {error && <ErrorBox message={error} onRetry={load} />}

      <Section
        title="Signal history"
        right={<button className="btn sm ghost" onClick={load}>{loading ? <Spinner /> : 'Refresh'}</button>}
        bodyStyle={{ padding: 0 }}
      >
        <div className="table-wrap" style={{ maxHeight: 420, overflowY: 'auto' }}>
          <table className="data">
            <thead>
              <tr><th>Fired</th><th>Symbol</th><th>TF</th><th>Strategy</th><th>Action</th><th className="num">Price</th><th className="num">Conf.</th><th className="num">Stop</th><th className="num">TP1</th></tr>
            </thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.id}>
                  <td className="mono muted">{fmtTime(s.created_at)}</td>
                  <td className="mono">{s.symbol}</td>
                  <td className="mono muted">{s.timeframe}</td>
                  <td className="muted">{s.strategy}</td>
                  <td><ActionChip action={s.action} /></td>
                  <td className="num">{fmtPrice(s.price)}</td>
                  <td className="num">{s.confidence}%</td>
                  <td className="num neg">{fmtPrice(s.plan?.stop)}</td>
                  <td className="num pos">{fmtPrice(s.plan?.take_profits?.[0] ?? null)}</td>
                </tr>
              ))}
              {signals.length === 0 && (
                <tr><td colSpan={9}><div className="empty">No signals stored yet. The scanner records actionable signals automatically.</div></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Alert delivery log" bodyStyle={{ padding: 0 }}>
        <div className="table-wrap" style={{ maxHeight: 320, overflowY: 'auto' }}>
          <table className="data">
            <thead><tr><th>Time</th><th>Channel</th><th>Status</th><th>Detail</th></tr></thead>
            <tbody>
              {notifications.map((n) => (
                <tr key={n.id}>
                  <td className="mono muted">{fmtTime(n.created_at)}</td>
                  <td className="mono">{n.channel}</td>
                  <td>
                    <span className={`chip ${n.status === 'sent' ? 'buy' : n.status === 'failed' ? 'sell' : 'wait'}`}>{n.status}</span>
                  </td>
                  <td className="muted" style={{ whiteSpace: 'normal', maxWidth: 520 }}>{n.detail || '—'}</td>
                </tr>
              ))}
              {notifications.length === 0 && (
                <tr><td colSpan={4}><div className="empty">No alert attempts yet.</div></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  )
}
