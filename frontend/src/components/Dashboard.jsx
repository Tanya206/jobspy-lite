import { useEffect, useRef, useState } from 'react'
import {
  exportUrl, getResumeInfo, getRunLatest, getScheduler, getSettings,
  getStats, saveSchedulerConfig, saveSettings, startRun, testNotify,
  toggleScheduler, uploadResume,
} from '../api.js'
import { formatIst } from '../datetime.js'
import { ErrorBanner, ProgressBar, Spinner } from './ui.jsx'

function StatCard({ label, value, accent }) {
  return (
    <div className={`stat-card ${accent ? 'stat-card--accent' : ''}`}>
      <div className="stat-card__value">{value ?? '—'}</div>
      <div className="stat-card__label">{label}</div>
    </div>
  )
}

export default function Dashboard({ onGoJobs }) {
  const [stats, setStats] = useState(null)
  const [resume, setResume] = useState(null)
  const [sched, setSched] = useState(null)
  const [settings, setSettings] = useState(null)
  const [run, setRun] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [showSchedForm, setShowSchedForm] = useState(false)
  const [schedForm, setSchedForm] = useState(null)
  const [notifyMsg, setNotifyMsg] = useState('')
  const fileRef = useRef(null)

  async function refreshAll() {
    try {
      const [s, r, sc, st, rl] = await Promise.all([
        getStats(), getResumeInfo(), getScheduler(), getSettings(), getRunLatest(),
      ])
      setStats(s)
      setResume(r)
      setSched(sc)
      setSettings(st)
      setRun(rl)
      setSchedForm((prev) => prev || sc.config)
      setError(null)
    } catch (e) {
      setError(e)
    }
  }

  useEffect(() => {
    refreshAll()
    const timer = setInterval(() => {
      getRunLatest().then(setRun).catch(() => {})
      getScheduler().then(setSched).catch(() => {})
      getStats().then(setStats).catch(() => {})
    }, 5000)
    return () => clearInterval(timer)
  }, [])

  async function onUpload(file) {
    if (!file) return
    setUploading(true)
    setError(null)
    try {
      const info = await uploadResume(file)
      setResume(info)
    } catch (e) {
      setError(e)
    } finally {
      setUploading(false)
    }
  }

  async function onRunNow(force = false) {
    setBusy(true)
    setError(null)
    try {
      await startRun(force)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  async function onToggleSched() {
    setBusy(true)
    setError(null)
    try {
      const next = await toggleScheduler(!sched.running)
      setSched(next)
      setSchedForm(next.config)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  async function onSaveSched() {
    setBusy(true)
    setError(null)
    try {
      const next = await saveSchedulerConfig(schedForm)
      setSched(next)
      setSchedForm(next.config)
      setShowSchedForm(false)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  async function onSaveSettings(patch) {
    setBusy(true)
    setError(null)
    try {
      const next = await saveSettings(patch)
      setSettings(next)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  async function onTestNotify() {
    setNotifyMsg('')
    try {
      const out = await testNotify()
      setNotifyMsg(out.sent ? `Sent ${out.sent} alert(s).` : 'Nothing pending or Telegram not configured.')
    } catch (e) {
      setNotifyMsg(String(e.message || e))
    }
  }

  const running = run?.running
  const schedOn = sched?.running

  return (
    <section>
      <p className="eyebrow" style={{ marginBottom: 12 }}>Overview</p>
      <ErrorBanner error={error} />

      <div className="stat-grid">
        <StatCard label="total jobs" value={stats?.total} />
        <StatCard label="new" value={stats?.new} accent />
        <StatCard label="accepted" value={stats?.accepted} />
        <StatCard label="rejected" value={stats?.rejected} />
        <StatCard label="avg score" value={stats?.avg_score} />
      </div>

      <div className="panel-grid">
        <div className="panel">
          <h3 className="panel__title">Résumé</h3>
          {resume?.name ? (
            <p className="small">
              <b>{resume.name}</b>
              {resume.chars ? ` · ${resume.chars.toLocaleString()} chars` : ''}
              {resume.uploaded_at ? ` · uploaded ${formatIst(resume.uploaded_at)}` : ''}
            </p>
          ) : (
            <p className="muted small">No resume stored yet — the pipeline needs one before it can score jobs.</p>
          )}
          <input
            ref={fileRef}
            type="file"
            accept=".docx"
            style={{ display: 'none' }}
            onChange={(e) => onUpload(e.target.files?.[0])}
          />
          <button className="btn btn--primary" disabled={uploading} onClick={() => fileRef.current?.click()}>
            {uploading ? <Spinner label="Uploading…" /> : resume?.name ? 'Replace résumé (.docx)' : 'Upload résumé (.docx)'}
          </button>
        </div>

        <div className="panel">
          <h3 className="panel__title">Cron scheduler</h3>
          <div className="sched-status">
            <span className={`status-dot ${schedOn ? 'status-dot--on' : 'status-dot--off'}`} />
            <span>{schedOn ? 'running' : 'stopped'}</span>
            {sched?.next_run_at ? (
              <span className="muted small">· next {formatIst(sched.next_run_at)}</span>
            ) : null}
          </div>
          {sched?.config ? (
            <p className="muted small">
              {sched.config.mode === 'cron'
                ? `cron: ${sched.config.cron_expression}`
                : `every ${sched.config.interval_minutes} min, ${sched.config.active_from}:00–${sched.config.active_to}:00`}
            </p>
          ) : null}
          <div className="toolbar">
            <button className={`btn ${schedOn ? '' : 'btn--primary'}`} disabled={busy} onClick={onToggleSched}>
              {schedOn ? 'Stop scheduler' : 'Start scheduler'}
            </button>
            <button className="btn" onClick={() => setShowSchedForm((v) => !v)}>
              {showSchedForm ? 'Hide config' : 'Configure'}
            </button>
          </div>
          {showSchedForm && schedForm ? (
            <SchedForm form={schedForm} setForm={setSchedForm} onSave={onSaveSched} busy={busy} />
          ) : null}
        </div>

        <div className="panel">
          <h3 className="panel__title">Scrape params</h3>
          {settings?.scrape_params ? (
            <>
              <label className="inline">
                search terms (comma-sep)
                <input
                  className="input" type="text"
                  defaultValue={(settings.scrape_params.search_terms || []).join(', ')}
                  onBlur={(e) => {
                    const terms = e.target.value.split(',').map((t) => t.trim()).filter(Boolean)
                    const current = (settings.scrape_params.search_terms || []).join(', ')
                    if (terms.join(', ') !== current) {
                      onSaveSettings({ scrape_params: { ...settings.scrape_params, search_terms: terms } })
                    }
                  }}
                />
              </label>
              <label className="inline">
                locations (comma-sep)
                <input
                  className="input" type="text"
                  title="One city per comma — each is scraped separately, in order."
                  defaultValue={settings.scrape_params.location ?? ''}
                  onBlur={(e) => {
                    const v = e.target.value.trim()
                    if (v !== (settings.scrape_params.location ?? '')) {
                      onSaveSettings({ scrape_params: { ...settings.scrape_params, location: v } })
                    }
                  }}
                />
              </label>
              <div className="toolbar">
                <label className="inline">
                  results
                  <input
                    className="input input--num" type="number" min="1"
                    defaultValue={settings.scrape_params.results_wanted ?? 600}
                    onBlur={(e) => {
                      const v = parseInt(e.target.value, 10)
                      if (!Number.isNaN(v) && v !== settings.scrape_params.results_wanted) {
                        onSaveSettings({ scrape_params: { ...settings.scrape_params, results_wanted: v } })
                      }
                    }}
                  />
                </label>
                <label className="inline">
                  hours old
                  <input
                    className="input input--num" type="number" min="1"
                    defaultValue={settings.scrape_params.hours_old ?? 3}
                    onBlur={(e) => {
                      const v = parseInt(e.target.value, 10)
                      if (!Number.isNaN(v) && v !== settings.scrape_params.hours_old) {
                        onSaveSettings({ scrape_params: { ...settings.scrape_params, hours_old: v } })
                      }
                    }}
                  />
                </label>
              </div>
              <div className="toolbar">
                <label className="inline">
                  sites
                  <input
                    className="input input--num" type="text"
                    defaultValue={(settings.scrape_params.sites || []).join(', ')}
                    onBlur={(e) => {
                      const sites = e.target.value.split(',').map((s) => s.trim()).filter(Boolean)
                      if (sites.join(', ') !== (settings.scrape_params.sites || []).join(', ')) {
                        onSaveSettings({ scrape_params: { ...settings.scrape_params, sites } })
                      }
                    }}
                  />
                </label>
                <label className="inline">
                  <input
                    type="checkbox"
                    defaultChecked={!!settings.scrape_params.linkedin_fetch_description}
                    onChange={(e) => onSaveSettings({ scrape_params: { ...settings.scrape_params, linkedin_fetch_description: e.target.checked } })}
                  />
                  full descriptions
                </label>
              </div>
              <p className="muted small">
                Changes apply from the next run. Comma-separated locations are scraped one city at a time.
              </p>
            </>
          ) : (
            <Spinner label="Loading settings…" />
          )}
        </div>

        <div className="panel">
          <h3 className="panel__title">AI agent</h3>
          {settings?.agent_params ? (
            <>
              <label className="inline">
                model
                <input
                  className="input" type="text"
                  defaultValue={settings.agent_params.ai_model ?? ''}
                  onBlur={(e) => {
                    const v = e.target.value.trim()
                    if (v && v !== settings.agent_params.ai_model) {
                      onSaveSettings({ agent_params: { ...settings.agent_params, ai_model: v } })
                    }
                  }}
                />
              </label>
              <label className="inline">
                base URL
                <input
                  className="input" type="text"
                  placeholder={settings.agent_params.ai_base_url || 'https://integrate.api.nvidia.com/v1'}
                  defaultValue={settings.agent_params.ai_base_url ?? ''}
                  onBlur={(e) => {
                    const v = e.target.value.trim()
                    if (v !== (settings.agent_params.ai_base_url ?? '')) {
                      onSaveSettings({ agent_params: { ...settings.agent_params, ai_base_url: v } })
                    }
                  }}
                />
              </label>
              <div className="toolbar">
                <label className="inline">
                  rate limit / min
                  <input
                    className="input input--num" type="number" min="0"
                    defaultValue={settings.agent_params.ai_rate_limit_per_min ?? 30}
                    onBlur={(e) => {
                      const v = parseInt(e.target.value, 10)
                      if (!Number.isNaN(v) && v !== settings.agent_params.ai_rate_limit_per_min) {
                        onSaveSettings({ agent_params: { ...settings.agent_params, ai_rate_limit_per_min: v } })
                      }
                    }}
                  />
                </label>
                <label className="inline">
                  workers
                  <input
                    className="input input--num" type="number" min="1" max="16"
                    defaultValue={settings.max_workers ?? 4}
                    onBlur={(e) => {
                      const v = parseInt(e.target.value, 10)
                      if (!Number.isNaN(v) && v !== settings.max_workers) {
                        onSaveSettings({ agent_params: { ...settings.agent_params, max_workers: v } })
                      }
                    }}
                  />
                </label>
              </div>
              <p className="muted small">Model/URL apply from the next run; rate limit and workers apply live. API keys stay in the server .env.</p>
            </>
          ) : (
            <Spinner label="Loading settings…" />
          )}
        </div>

        <div className="panel">
          <h3 className="panel__title">Screening</h3>
          {settings ? (
            <>
              <label className="inline">
                score threshold
                <input
                  className="input input--num"
                  type="number" min="0" max="100"
                  defaultValue={settings.score_threshold}
                  onBlur={(e) => {
                    const v = parseInt(e.target.value, 10)
                    if (!Number.isNaN(v) && v !== settings.score_threshold) onSaveSettings({ score_threshold: v })
                  }}
                />
              </label>
              <label className="inline">
                exp. min yrs
                <input
                  className="input input--num"
                  type="number" min="0"
                  defaultValue={settings.experience_min_years ?? 0}
                  onBlur={(e) => {
                    const v = parseInt(e.target.value, 10)
                    if (!Number.isNaN(v) && v !== settings.experience_min_years) onSaveSettings({ experience_min_years: v })
                  }}
                />
              </label>
              <label className="inline">
                exp. max yrs
                <input
                  className="input input--num"
                  type="number" min="0"
                  defaultValue={settings.experience_max_years ?? ''}
                  onBlur={(e) => {
                    const raw = e.target.value.trim()
                    const v = raw === '' ? 0 : parseInt(raw, 10)
                    if (!Number.isNaN(v) && v !== settings.experience_max_years) onSaveSettings({ experience_max_years: v })
                  }}
                />
              </label>
              <p className="muted small">Blank max = open-ended. Changes apply from the next run.</p>
            </>
          ) : (
            <Spinner label="Loading settings…" />
          )}
        </div>
      </div>

      <div className="panel">
        <h3 className="panel__title">Pipeline run</h3>
        <div className="toolbar">
          <button className="btn btn--primary" disabled={busy || running} onClick={() => onRunNow(false)}>
            {running ? <Spinner label="Running…" /> : 'Run now'}
          </button>
          <button className="btn" disabled={busy || running} onClick={() => onRunNow(true)}>
            Force re-score
          </button>
          <button className="btn" onClick={onTestNotify} title="Send any pending above-threshold matches to Telegram">
            Test Telegram alert
          </button>
          <a className="btn" href={exportUrl()} download>
            Download Excel
          </a>
          <button className="btn" onClick={onGoJobs}>View jobs →</button>
        </div>
        {running ? (
          <div style={{ marginTop: 10 }}>
            <ProgressBar pct={run?.percent} />
            <p className="muted small">{run?.progress || 'Working…'}</p>
          </div>
        ) : null}
        {run?.summary ? (
          <p className="small" style={{ marginTop: 8 }}>
            Last manual run: <b>{run.summary.status}</b>
            {typeof run.summary.jobs_scraped === 'number' ? ` · ${run.summary.jobs_scraped} scraped` : ''}
            {typeof run.summary.scored === 'number' ? ` · ${run.summary.scored} scored` : ''}
            {typeof run.summary.alerts_sent === 'number' ? ` · ${run.summary.alerts_sent} alerts` : ''}
            {run.summary.error ? ` · ${run.summary.error}` : ''}
          </p>
        ) : null}
        {notifyMsg ? <p className="small" style={{ marginTop: 6 }}>{notifyMsg}</p> : null}
      </div>
    </section>
  )
}

function SchedForm({ form, setForm, onSave, busy }) {
  function set(key, value) {
    setForm((f) => ({ ...f, [key]: value }))
  }
  return (
    <div className="sched-form">
      <label className="inline">
        mode
        <select
          className="select"
          value={form.mode}
          onChange={(e) => set('mode', e.target.value)}
        >
          <option value="interval">interval</option>
          <option value="cron">cron expression</option>
        </select>
      </label>
      {form.mode === 'interval' ? (
        <>
          <label className="inline">
            every (min)
            <input
              className="input input--num" type="number" min="5"
              value={form.interval_minutes}
              onChange={(e) => set('interval_minutes', Number(e.target.value))}
            />
          </label>
          <label className="inline">
            active from (h)
            <input
              className="input input--num" type="number" min="0" max="23"
              value={form.active_from}
              onChange={(e) => set('active_from', Number(e.target.value))}
            />
          </label>
          <label className="inline">
            active to (h)
            <input
              className="input input--num" type="number" min="1" max="24"
              value={form.active_to}
              onChange={(e) => set('active_to', Number(e.target.value))}
            />
          </label>
        </>
      ) : (
        <label className="inline">
          cron (min hour day month weekday)
          <input
            className="select" type="text"
            value={form.cron_expression}
            onChange={(e) => set('cron_expression', e.target.value)}
            placeholder="0 */2 * * *"
          />
        </label>
      )}
      <div className="toolbar">
        <button className="btn btn--primary" disabled={busy} onClick={onSave}>Save schedule</button>
      </div>
    </div>
  )
}
