import { useEffect, useState } from 'react'
import { getHealth, getToken, setToken, setUnauthorizedHandler } from './api.js'
import ApplicationsView from './components/ApplicationsView.jsx'
import Dashboard from './components/Dashboard.jsx'
import JobsView from './components/JobsView.jsx'
import RunsView from './components/RunsView.jsx'

const TABS = [
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'jobs', label: 'Jobs' },
  { key: 'runs', label: 'Runs' },
  { key: 'applications', label: 'Applications' },
]

export default function App() {
  const [tab, setTab] = useState('dashboard')
  const [health, setHealth] = useState(null)
  const [needsToken, setNeedsToken] = useState(false)
  const [tokenDraft, setTokenDraft] = useState(() => getToken())

  useEffect(() => {
    setUnauthorizedHandler(() => setNeedsToken(true))
    return () => setUnauthorizedHandler(null)
  }, [])

  useEffect(() => {
    let alive = true
    const poll = () =>
      getHealth()
        .then((h) => alive && setHealth(h))
        .catch(() => alive && setHealth({ status: 'unreachable' }))
    poll()
    const timer = setInterval(poll, 30000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])

  const jobsNav = () => setTab('jobs')

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar__top">
          <div className="brand">
            <span className="brand__mark" aria-hidden>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor">
                <circle cx="12" cy="12" r="9" strokeWidth="1.5" opacity="0.5" />
                <circle cx="12" cy="12" r="5" strokeWidth="1.5" />
                <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
                <path d="M12 1.5v4M12 18.5v4M1.5 12h4M18.5 12h4" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </span>
            <div>
              <h1 className="brand__name">Job<b>Spy</b></h1>
              <p className="brand__tag">Lite</p>
            </div>
          </div>
          <ThemeToggle />
        </div>

        <nav className="sidebar__nav" aria-label="Pages">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={`nav-item ${tab === t.key ? 'nav-item--active' : ''}`}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <div className="sidebar__foot">
          <span
            className={`health-pill ${health?.status === 'ok' ? 'health-pill--ok' : 'health-pill--bad'}`}
            title={health ? JSON.stringify(health, null, 1) : 'checking…'}
          >
            <span className="health-dot" />
            {health?.status === 'ok' ? 'Backend ok' : 'Backend unreachable'}
          </span>
        </div>
      </aside>

      <div className="workspace">
        {needsToken ? (
          <form
            className="token-bar"
            onSubmit={(event) => {
              event.preventDefault()
              setToken(tokenDraft.trim())
              setNeedsToken(false)
              window.location.reload()
            }}
          >
            <label className="inline">
              API token
              <input
                className="input"
                type="password"
                value={tokenDraft}
                onChange={(event) => setTokenDraft(event.target.value)}
                placeholder="JOBSPY_API_TOKEN"
                autoComplete="off"
              />
            </label>
            <button className="btn btn--primary" type="submit">Save token</button>
          </form>
        ) : null}

        <main className="main">
          {tab === 'dashboard' ? <Dashboard onGoJobs={jobsNav} /> : null}
          {tab === 'jobs' ? <JobsView /> : null}
          {tab === 'runs' ? <RunsView /> : null}
          {tab === 'applications' ? <ApplicationsView /> : null}
        </main>
      </div>
    </div>
  )
}

function ThemeToggle() {
  const [dark, setDark] = useState(() => {
    try {
      return localStorage.getItem('jobspy-lite.theme') !== 'light'
    } catch {
      return true
    }
  })
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light'
    try {
      localStorage.setItem('jobspy-lite.theme', dark ? 'dark' : 'light')
    } catch {
      /* private mode */
    }
  }, [dark])
  return (
    <button className="icon-btn" onClick={() => setDark((v) => !v)} aria-label="Toggle theme" title={dark ? 'Switch to light' : 'Switch to dark'}>
      {dark ? '☾' : '☀'}
    </button>
  )
}
