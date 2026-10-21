import { useEffect, useState } from 'react'
import { getHealth, getToken, setToken, setUnauthorizedHandler } from './api.js'
import AnalyticsView from './components/AnalyticsView.jsx'
import ApplicationsView from './components/ApplicationsView.jsx'
import Dashboard from './components/Dashboard.jsx'
import JobsView from './components/JobsView.jsx'

const NAV = [
  { key: 'jobs', label: 'Jobs', icon: IconJobs },
  { key: 'analytics', label: 'Analytics', icon: IconChart },
  { key: 'applications', label: 'Applications', icon: IconList },
]

export default function App() {
  const [tab, setTab] = useState('jobs')
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

  useEffect(() => {
    window.scrollTo(0, 0)
  }, [tab])

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
          {NAV.map((item) => (
            <button
              key={item.key}
              className={`nav-item ${tab === item.key ? 'nav-item--active' : ''}`}
              onClick={() => setTab(item.key)}
            >
              <item.icon />
              {item.label}
            </button>
          ))}
        </nav>

        <div className="sidebar__foot">
          <button
            className={`nav-item ${tab === 'settings' ? 'nav-item--active' : ''}`}
            onClick={() => setTab('settings')}
          >
            <IconSettings />
            Settings
          </button>
          <span
            className={`health-pill ${health?.status === 'ok' ? 'health-pill--ok' : 'health-pill--bad'}`}
            title={health ? JSON.stringify(health, null, 1) : 'checking…'}
          >
            <span className="health-dot" />
            {health?.status === 'ok' ? 'Connected' : 'Offline'}
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
          {tab === 'jobs' ? <JobsView /> : null}
          {tab === 'analytics' ? <AnalyticsView /> : null}
          {tab === 'applications' ? <ApplicationsView /> : null}
          {tab === 'settings' ? <Dashboard onGoJobs={jobsNav} /> : null}
        </main>
      </div>
    </div>
  )
}

function IconJobs() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <rect x="3" y="7" width="18" height="13" rx="2" />
      <path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" strokeLinecap="round" />
    </svg>
  )
}

function IconChart() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <path d="M4 19V5M4 19h16" strokeLinecap="round" />
      <path d="M8 15l4-5 3 3 5-7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function IconList() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <path d="M8 7h12M8 12h12M8 17h12" strokeLinecap="round" />
      <path d="M4 7h.01M4 12h.01M4 17h.01" strokeLinecap="round" />
    </svg>
  )
}

function IconSettings() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3v2.2M12 18.8V21M3 12h2.2M18.8 12H21M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6" strokeLinecap="round" />
    </svg>
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
