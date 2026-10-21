import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import BacktestView from './components/BacktestView'
import CandleChart from './components/CandleChart'
import HistoryView from './components/HistoryView'
import OscillatorChart from './components/OscillatorChart'
import SettingsView from './components/SettingsView'
import SetupRadar from './components/SetupRadar'
import WinRateView from './components/WinRateView'
import SignalPanel from './components/SignalPanel'
import WatchlistPanel from './components/WatchlistPanel'
import { ErrorBox, Spinner, fmtAgo, fmtPrice } from './components/ui'
import type { Meta, ScanRow, Settings, Signal } from './types'

type Tab = 'radar' | 'dashboard' | 'winrate' | 'backtest' | 'history' | 'settings'

const TABS: { id: Tab; label: string }[] = [
  { id: 'radar', label: 'Setup radar' },
  { id: 'dashboard', label: 'Chart' },
  { id: 'winrate', label: 'Win rate' },
  { id: 'backtest', label: 'Backtest' },
  { id: 'history', label: 'Signals & alerts' },
  { id: 'settings', label: 'Settings' },
]

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [tab, setTab] = useState<Tab>('radar')
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [timeframe, setTimeframe] = useState('1h')
  const [strategyId, setStrategyId] = useState('donchian')

  const [signal, setSignal] = useState<Signal | null>(null)
  const [scanRows, setScanRows] = useState<ScanRow[]>([])
  const [loadingSignal, setLoadingSignal] = useState(true)
  const [loadingScan, setLoadingScan] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [bootError, setBootError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [tick, setTick] = useState(0)
  const requestId = useRef(0)

  /* ------------------------------------------------------------- bootstrap */
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const data = await api.meta()
        if (cancelled) return
        setMeta(data)
        setSettings(data.settings)
        setSymbol(data.settings.watchlist[0] ?? 'BTCUSDT')
        setTimeframe(data.settings.timeframe)
        setStrategyId(data.settings.strategy)
      } catch (err) {
        if (!cancelled) setBootError(err instanceof Error ? err.message : 'Could not reach the API')
      }
    })()
    return () => { cancelled = true }
  }, [])

  /* ---------------------------------------------------------------- fetch */
  const loadSignal = useCallback(async (sym: string, tf: string, strat: string) => {
    const id = ++requestId.current
    setLoadingSignal(true)
    try {
      const data = await api.signal(sym, tf, strat)
      if (id !== requestId.current) return
      setSignal(data)
      setError(null)
      setUpdatedAt(Date.now())
    } catch (err) {
      if (id === requestId.current) setError(err instanceof Error ? err.message : 'Signal request failed')
    } finally {
      if (id === requestId.current) setLoadingSignal(false)
    }
  }, [])

  const loadScan = useCallback(async (watchlist: string[], tf: string, strat: string) => {
    setLoadingScan(true)
    try {
      const data = await api.scan(watchlist, tf, strat)
      setScanRows(data.rows)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Scan failed')
    } finally {
      setLoadingScan(false)
    }
  }, [])

  useEffect(() => {
    if (!meta) return
    loadSignal(symbol, timeframe, strategyId)
  }, [meta, symbol, timeframe, strategyId, loadSignal])

  useEffect(() => {
    if (!meta || !settings) return
    loadScan(settings.watchlist, timeframe, strategyId)
  }, [meta, settings, timeframe, strategyId, loadScan])

  // Polling keeps the dashboard live without a websocket.
  useEffect(() => {
    if (!meta || !settings || tab !== 'dashboard') return
    const interval = Math.max(20, settings.refresh_seconds) * 1000
    const timer = setInterval(() => {
      loadSignal(symbol, timeframe, strategyId)
      loadScan(settings.watchlist, timeframe, strategyId)
    }, interval)
    const ticker = setInterval(() => setTick((t) => t + 1), 1000)
    return () => { clearInterval(timer); clearInterval(ticker) }
  }, [meta, settings, tab, symbol, timeframe, strategyId, loadSignal, loadScan])

  const refresh = () => {
    if (!settings) return
    loadSignal(symbol, timeframe, strategyId)
    loadScan(settings.watchlist, timeframe, strategyId)
  }

  const onWatchlistSaved = (next: Settings) => setSettings(next)

  const secondsToRefresh = useMemo(() => {
    if (!updatedAt || !settings) return null
    const elapsed = Math.floor((Date.now() - updatedAt) / 1000)
    return Math.max(0, settings.refresh_seconds - elapsed)
  }, [updatedAt, settings, tick])

  const baseSymbol = symbol.replace('USDT', '')

  /* ------------------------------------------------------------- rendering */
  if (bootError) {
    return (
      <div style={{ maxWidth: 620, margin: '80px auto', padding: 24 }}>
        <h2 style={{ marginBottom: 8 }}>Cannot reach the API</h2>
        <p className="muted small" style={{ marginBottom: 16 }}>
          Start the backend on port 8000, then reload. In development the Vite server proxies <code>/api</code> to it.
        </p>
        <ErrorBox message={bootError} onRetry={() => window.location.reload()} />
      </div>
    )
  }

  if (!meta || !settings) {
    return (
      <div style={{ display: 'grid', placeItems: 'center', height: '100vh', gap: 12 }}>
        <div className="hstack"><Spinner /><span className="muted">Connecting to SignalForge…</span></div>
      </div>
    )
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
              <path d="M3 17l5-6 4 3.5L21 4" stroke="#2fd6a3" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M15 4h6v6" stroke="#2fd6a3" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <div>
            <div className="brand-name">Signal<span>Forge</span></div>
            <div className="brand-sub">Strategy signal engine</div>
          </div>
        </div>

        <nav className="tabs">
          {TABS.map((t) => (
            <button key={t.id} className={`tab ${tab === t.id ? 'active' : ''}`} onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </nav>

        <div className="topbar-right">
          <span className={`badge ${meta.provider.mode === 'live' ? 'live' : 'demo'}`} title={meta.provider.reason}>
            <span className={`dot ${meta.provider.mode === 'live' ? 'pulse' : ''}`} />
            {meta.provider.mode === 'live' ? 'Live market data' : 'Demo data'}
          </span>
          {tab === 'dashboard' && (
            <>
              <select className="select" value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>
                {meta.strategies.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} · v{s.version}{s.frozen ? ' (frozen)' : ''}
                  </option>
                ))}
              </select>
              <div className="segmented">
                {(meta.scan_timeframes ?? meta.timeframes.map((t) => t.value)).map((tf) => (
                  <button key={tf} className={timeframe === tf ? 'active' : ''} onClick={() => setTimeframe(tf)}>
                    {tf}
                  </button>
                ))}
              </div>
              <button className="btn sm" onClick={refresh} disabled={loadingSignal}>
                {loadingSignal ? <Spinner /> : 'Refresh'}
              </button>
              <span className="tiny muted mono" style={{ minWidth: 78, textAlign: 'right' }}>
                {secondsToRefresh != null ? `next ${secondsToRefresh}s` : ''}
              </span>
            </>
          )}
        </div>
      </header>

      {tab === 'dashboard' && (
        <main className="layout">
          <WatchlistPanel rows={scanRows} selected={symbol} loading={loadingScan} onSelect={setSymbol} />

          <div className="stack" style={{ gap: 16 }}>
            {error && <ErrorBox message={error} onRetry={refresh} />}

            {signal ? (
              <>
                <div className="hero">
                  <SignalPanel signal={signal} />
                  <div className="stack" style={{ gap: 16 }}>
                    <section className="panel">
                      <header className="panel-head">
                        <span className="panel-title">{baseSymbol} price action</span>
                        <span className="right tiny muted mono">{signal.timeframe_label}</span>
                      </header>
                      <div className="panel-body" style={{ padding: '10px 12px 4px' }}>
                        <CandleChart chart={signal.chart} height={signal.chart.pane ? 330 : 366} />
                        {signal.chart.pane && (
                          <div style={{ borderTop: '1px solid var(--border-soft)', marginTop: 6, paddingTop: 6 }}>
                            <OscillatorChart pane={signal.chart.pane} bars={signal.chart.time.length} />
                          </div>
                        )}
                      </div>
                    </section>

                    <section className="panel">
                      <header className="panel-head">
                        <span className="panel-title">Indicator snapshot</span>
                        <span className="right tiny muted mono">{signal.source === 'demo' ? 'synthetic feed' : 'Binance feed'}</span>
                      </header>
                      <div className="panel-body" style={{ padding: 12 }}>
                        <div className="grid-3" style={{ gap: 8 }}>
                          {Object.entries(signal.indicators).slice(0, 9).map(([key, value]) => (
                            <div className="plan-box" key={key} style={{ padding: '8px 10px' }}>
                              <div className="k">{key.replace(/_/g, ' ')}</div>
                              <div className="v" style={{ fontSize: 13 }}>{fmtPrice(value)}</div>
                            </div>
                          ))}
                        </div>
                        <div className="row-between" style={{ marginTop: 12 }}>
                          <span className="tiny muted">Last closed bar</span>
                          <span className="tiny mono muted">{fmtAgo(signal.bar_time)}</span>
                        </div>
                      </div>
                    </section>
                  </div>
                </div>

                <section className="panel">
                  <header className="panel-head">
                    <span className="panel-title">How this signal is made</span>
                    <span className="right tiny muted">{signal.strategy.name}</span>
                  </header>
                  <div className="panel-body small muted" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))', gap: 14 }}>
                    <div>
                      <strong style={{ color: 'var(--text)' }}>{signal.strategy.name}</strong> evaluates every closed
                      {` `}{signal.timeframe} candle. The rule checks above are the exact conditions that produced the
                      current {signal.action.toLowerCase()} call.
                    </div>
                    <div>
                      Entry, stop and targets come from the strategy's own risk model (ATR-based), not from a fixed
                      percentage. Risk shown is the distance to the stop as a share of price.
                    </div>
                    <div>
                      The scanner re-runs this on your whole watchlist every {settings.refresh_seconds}s and stores each
                      actionable signal once per bar, then fires Telegram / webhook alerts.
                    </div>
                  </div>
                </section>
              </>
            ) : (
              <div className="panel empty" style={{ display: 'grid', placeItems: 'center', minHeight: 320 }}>
                <div className="hstack"><Spinner /><span>Loading {baseSymbol} signals…</span></div>
              </div>
            )}
          </div>
        </main>
      )}

      {tab === 'radar' && (
        <main className="layout single">
          <SetupRadar
            meta={meta}
            watchlist={settings.watchlist}
            strategyId={strategyId}
            onOpen={(sym, tf) => { setSymbol(sym); setTimeframe(tf); setTab('dashboard') }}
          />
        </main>
      )}

      {tab === 'winrate' && (
        <main className="layout single">
          <WinRateView meta={meta} />
        </main>
      )}

      {tab === 'backtest' && (
        <main className="layout single">
          <BacktestView meta={meta} initialSymbol={symbol} initialTimeframe={timeframe} initialStrategy={strategyId} />
        </main>
      )}

      {tab === 'history' && (
        <main className="layout single">
          <HistoryView />
        </main>
      )}

      {tab === 'settings' && (
        <main className="layout single">
          <SettingsView meta={meta} settings={settings} onSaved={onWatchlistSaved} />
        </main>
      )}

      <footer className="footer">
        SignalForge · research tool, not financial advice. Signals are generated from historical price data
        {meta.provider.mode === 'demo' ? ' — current feed is simulated demo data.' : '.'}
      </footer>
    </div>
  )
}
