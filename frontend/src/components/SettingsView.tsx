import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Meta, Settings } from '../types'
import ParamsEditor from './ParamsEditor'
import { ErrorBox, Field, Section, Switch, fmtTime } from './ui'

interface Props {
  meta: Meta
  settings: Settings
  onSaved: (settings: Settings) => void
}

export default function SettingsView({ meta, settings, onSaved }: Props) {
  const [draft, setDraft] = useState<Settings>(settings)
  const [newSymbol, setNewSymbol] = useState('')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ channel: string; status: string; detail: string } | null>(null)
  const [strategyParams, setStrategyParams] = useState<Record<string, number | boolean>>({})

  const schema = meta.strategies.find((s) => s.id === draft.strategy) ?? meta.strategies[0]

  useEffect(() => {
    setDraft(settings)
    const active = meta.strategies.find((s) => s.id === settings.strategy)
    if (active) setStrategyParams({ ...active.defaults })
  }, [settings, meta])

  const save = async (patch?: Partial<Settings>) => {
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      const updated = await api.saveSettings(patch ?? draft)
      onSaved(updated)
      setDraft(updated)
      setMessage('Settings saved. The background scanner picks them up on its next pass.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save settings')
    } finally {
      setSaving(false)
    }
  }

  const addSymbol = () => {
    const symbol = newSymbol.trim().toUpperCase().replace('/', '')
    if (!symbol) return
    if (draft.watchlist.includes(symbol)) {
      setNewSymbol('')
      return
    }
    const next = [...draft.watchlist, symbol].slice(0, 25)
    setDraft({ ...draft, watchlist: next })
    setNewSymbol('')
  }

  const removeSymbol = (symbol: string) => setDraft({ ...draft, watchlist: draft.watchlist.filter((s) => s !== symbol) })

  const runTest = async (channel: 'telegram' | 'webhook') => {
    setTestResult(null)
    try {
      await save()
      const result = await api.testAlert(channel)
      setTestResult(result)
    } catch (err) {
      setTestResult({ channel, status: 'failed', detail: err instanceof Error ? err.message : 'Test failed' })
    }
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      {error && <ErrorBox message={error} />}
      {message && <div className="info-box">{message}</div>}

      <div className="grid-2" style={{ alignItems: 'start' }}>
        <Section title="Market & strategy">
          <div className="stack">
            <Field label="Default strategy" hint="Used by the dashboard, scanner and alerts.">
              <select
                className="select"
                value={draft.strategy}
                onChange={(e) => {
                  const next = { ...draft, strategy: e.target.value }
                  setDraft(next)
                  const found = meta.strategies.find((s) => s.id === e.target.value)
                  if (found) setStrategyParams({ ...found.defaults })
                }}
              >
                {meta.strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </Field>
            <Field label="Default timeframe">
              <select className="select" value={draft.timeframe} onChange={(e) => setDraft({ ...draft, timeframe: e.target.value })}>
                {meta.timeframes.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
              </select>
            </Field>
            <Field label="Scanner interval (seconds)" hint="How often the background scanner evaluates the watchlist. Minimum 20s.">
              <input
                className="input mono"
                type="number"
                min={20}
                max={3600}
                value={draft.refresh_seconds}
                onChange={(e) => setDraft({ ...draft, refresh_seconds: Number(e.target.value) })}
              />
            </Field>
            <div className="row-between" style={{ paddingTop: 4 }}>
              <span className="tiny muted">Data source · {meta.provider.reason}</span>
              <button className="btn primary sm" onClick={() => save()} disabled={saving}>{saving ? 'Saving…' : 'Save settings'}</button>
            </div>
          </div>
        </Section>

        <Section title={`Watchlist (${draft.watchlist.length}/25)`}>
          <div className="stack">
            <div className="chips-input">
              {draft.watchlist.map((symbol) => (
                <span className="chip-item" key={symbol}>
                  {symbol}
                  <button onClick={() => removeSymbol(symbol)} aria-label={`Remove ${symbol}`}>×</button>
                </span>
              ))}
              {draft.watchlist.length === 0 && <span className="tiny muted">Add at least one symbol</span>}
            </div>
            <div className="hstack">
              <input
                className="input sm mono"
                style={{ flex: 1 }}
                placeholder="e.g. PEPEUSDT"
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && addSymbol()}
              />
              <button className="btn sm" onClick={addSymbol}>Add</button>
            </div>
            <div className="tiny muted">Any Binance spot pair works. Quick picks:</div>
            <div className="hstack" style={{ gap: 6 }}>
              {meta.symbols.slice(0, 8).map((s) => (
                <button
                  key={s.symbol}
                  className="btn sm ghost"
                  disabled={draft.watchlist.includes(s.symbol)}
                  onClick={() => setDraft({ ...draft, watchlist: [...draft.watchlist, s.symbol].slice(0, 25) })}
                >
                  +{s.base}
                </button>
              ))}
            </div>
          </div>
        </Section>
      </div>

      <Section
        title="Alerts"
        right={<Switch checked={draft.alerts.enabled} onChange={(v) => setDraft({ ...draft, alerts: { ...draft.alerts, enabled: v } })} label="Scanner alerts" />}
      >
        <div className="stack">
          <div className="grid-3">
            <Field label="Minimum confidence to alert">
              <input
                className="input mono"
                type="number"
                min={0}
                max={100}
                value={draft.alerts.min_confidence}
                onChange={(e) => setDraft({ ...draft, alerts: { ...draft.alerts, min_confidence: Number(e.target.value) } })}
              />
            </Field>
            <Field label="Telegram bot token" hint="From @BotFather. Stored locally in SQLite.">
              <input
                className="input mono"
                type="password"
                placeholder={draft.alerts.telegram_configured ? '•••••••• (saved)' : '123456:ABC…'}
                value={draft.alerts.telegram_bot_token.startsWith('•') ? '' : draft.alerts.telegram_bot_token}
                onChange={(e) => setDraft({ ...draft, alerts: { ...draft.alerts, telegram_bot_token: e.target.value } })}
              />
            </Field>
            <Field label="Telegram chat id">
              <input
                className="input mono"
                placeholder="-100123456789"
                value={draft.alerts.telegram_chat_id}
                onChange={(e) => setDraft({ ...draft, alerts: { ...draft.alerts, telegram_chat_id: e.target.value } })}
              />
            </Field>
          </div>
          <Field label="Webhook URL" hint="Any endpoint that accepts a JSON POST — Slack, Discord, n8n, your own bot.">
            <input
              className="input mono"
              placeholder="https://hooks.example.com/signal"
              value={draft.alerts.webhook_url}
              onChange={(e) => setDraft({ ...draft, alerts: { ...draft.alerts, webhook_url: e.target.value } })}
            />
          </Field>
          <div className="hstack">
            <Switch
              checked={draft.alerts.telegram_enabled}
              onChange={(v) => setDraft({ ...draft, alerts: { ...draft.alerts, telegram_enabled: v } })}
              label="Send Telegram alerts"
            />
            <span style={{ flex: 1 }} />
            <button className="btn sm" onClick={() => runTest('telegram')}>Test Telegram</button>
            <button className="btn sm" onClick={() => runTest('webhook')}>Test webhook</button>
            <button className="btn primary sm" onClick={() => save()} disabled={saving}>{saving ? 'Saving…' : 'Save settings'}</button>
          </div>
          {testResult && (
            <div className={testResult.status === 'sent' ? 'info-box' : 'error-box'}>
              <strong>{testResult.channel}</strong>: {testResult.status} — {testResult.detail}
              {testResult.status !== 'sent' && (
                <div className="tiny" style={{ marginTop: 6 }}>
                  This sandbox has no outbound internet, so delivery tests fail here. On your machine or a VPS the same
                  call goes straight to Telegram/webhook.
                </div>
              )}
            </div>
          )}
        </div>
      </Section>

      <Section title="Strategy parameters" right={<span className="tiny muted">Preview of defaults used by the scanner</span>}>
        <ParamsEditor schema={schema} values={strategyParams} onChange={setStrategyParams} compact />
        <div className="tiny muted" style={{ marginTop: 10 }}>
          Per-run overrides live in the Backtester. Saved defaults come from {`backend/app/core/strategies/${schema.id}.py`}.
        </div>
      </Section>

      <div className="tiny muted" style={{ textAlign: 'center' }}>
        {meta.app} v{meta.version} · data {meta.provider.mode} · last loaded {fmtTime(Date.now())}
      </div>
    </div>
  )
}
