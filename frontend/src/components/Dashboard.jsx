import { useEffect, useRef, useState } from 'react'
import {
  exportUrl, getResumeInfo, getRunLatest, getScheduler, getSettings,
  getStats, purgeOlderThan, saveSchedulerConfig, saveSettings, startRun, testNotify,
  toggleScheduler, uploadResume,
} from '../api.js'
import { formatIst } from '../datetime.js'
import { ErrorBanner, ProgressBar, Spinner } from './ui.jsx'

const AI_PROVIDERS = {
  nvidia: {
    model: 'nvidia/nemotron-3-super-120b-a12b',
    baseUrl: 'https://integrate.api.nvidia.com/v1',
  },
  openai: {
    model: 'gpt-5-nano',
    baseUrl: 'https://api.openai.com/v1',
  },
}

function blurOnEnter(event) {
  if (event.key === 'Enter') event.currentTarget.blur()
}

function providerOf(params) {
  const url = String(params?.ai_base_url || '').toLowerCase()
  if (url.includes('api.openai.com')) return 'openai'
  if (url.includes('api.nvidia.com')) return 'nvidia'
  if (!url && (params?.ai_provider === 'openai' || params?.ai_provider === 'nvidia')) {
    return params.ai_provider
  }
  if (!url) return 'nvidia'
  return 'custom'
}

function StatCard({ label, value, tone }) {
  return (
    <div className={`stat-card${tone ? ` stat-card--${tone}` : ''}`}>
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
  const [purgeDays, setPurgeDays] = useState(30)
  const [purgeMsg, setPurgeMsg] = useState('')
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

  async function onPurge() {
    const days = parseInt(purgeDays, 10)
    if (!Number.isFinite(days) || days < 1) {
      setPurgeMsg('Enter at least 1 day.')
      return
    }
    const ok = window.confirm(
      `Delete jobs and run history older than ${days} days? Accepted jobs are kept.`,
    )
    if (!ok) return
    setBusy(true)
    setError(null)
    setPurgeMsg('')
    try {
      const result = await purgeOlderThan(days)
      setPurgeMsg(`Deleted ${result.jobs_deleted} job(s) and ${result.runs_deleted} run(s).`)
      await refreshAll()
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
        <StatCard label="total jobs" value={stats?.total} tone="accent" />
        <StatCard label="new" value={stats?.new} tone="info" />
        <StatCard label="accepted" value={stats?.accepted} tone="ok" />
        <StatCard label="rejected (manual)" value={stats?.manual_rejected} tone="no" />
        <StatCard label="auto-rejected" value={stats?.auto_rejected} tone="warn" />
        <StatCard label="avg score" value={stats?.avg_score} tone="accent" />
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
              <label className="stack-field">
                search terms (comma-sep)
                <input
                  className="input" type="text"
                  key={(settings.scrape_params.search_terms || []).join(', ')}
                  defaultValue={(settings.scrape_params.search_terms || []).join(', ')}
                  onKeyDown={blurOnEnter}
                  onBlur={(e) => {
                    const terms = e.target.value.split(',').map((t) => t.trim()).filter(Boolean)
                    const current = (settings.scrape_params.search_terms || []).join(', ')
                    if (terms.join(', ') !== current) {
                      onSaveSettings({ scrape_params: { ...settings.scrape_params, search_terms: terms } })
                    }
                  }}
                />
              </label>
              <label className="stack-field">
                locations (comma-sep)
                <input
                  className="input" type="text"
                  title="One city per comma — each is scraped separately, in order."
                  key={settings.scrape_params.location ?? ''}
                  defaultValue={settings.scrape_params.location ?? ''}
                  onKeyDown={blurOnEnter}
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
                    className="input input--num" type="number" min="1" max="1000"
                    key={settings.scrape_params.results_wanted ?? 600}
                    defaultValue={settings.scrape_params.results_wanted ?? 600}
                    onKeyDown={blurOnEnter}
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
                    className="input input--num" type="number" min="1" max="168"
                    key={settings.scrape_params.hours_old ?? 2}
                    defaultValue={settings.scrape_params.hours_old ?? 2}
                    onKeyDown={blurOnEnter}
                    onBlur={(e) => {
                      const v = parseInt(e.target.value, 10)
                      if (!Number.isNaN(v) && v !== settings.scrape_params.hours_old) {
                        onSaveSettings({ scrape_params: { ...settings.scrape_params, hours_old: v } })
                      }
                    }}
                  />
                </label>
              </div>
              <label className="stack-field">
                sites
                <input
                  className="input" type="text"
                  key={(settings.scrape_params.sites || []).join(', ')}
                  defaultValue={(settings.scrape_params.sites || []).join(', ')}
                  onKeyDown={blurOnEnter}
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
                  key={settings.scrape_params.linkedin_fetch_description ? 'desc-on' : 'desc-off'}
                  defaultChecked={!!settings.scrape_params.linkedin_fetch_description}
                  onChange={(e) => onSaveSettings({ scrape_params: { ...settings.scrape_params, linkedin_fetch_description: e.target.checked } })}
                />
                full descriptions
              </label>
              <p className="muted small">
                Results stay between 1 and 1000. Hours old stay between 1 and 168.
                Press Enter or click away to save. Changes apply from the next run.
                Comma-separated locations are scraped one city at a time.
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
                provider
                <select
                  className="select"
                  value={providerOf(settings.agent_params)}
                  onChange={(e) => {
                    const id = e.target.value
                    const preset = AI_PROVIDERS[id]
                    if (!preset) return
                    onSaveSettings({
                      agent_params: {
                        ...settings.agent_params,
                        ai_provider: id,
                        ai_model: preset.model,
                        ai_base_url: preset.baseUrl,
                      },
                    })
                  }}
                >
                  <option value="nvidia">NVIDIA NIM</option>
                  <option value="openai">OpenAI</option>
                  {providerOf(settings.agent_params) === 'custom' ? (
                    <option value="custom">Custom</option>
                  ) : null}
                </select>
              </label>
              <label className="inline">
                model
                <input
                  className="input" type="text"
                  key={settings.agent_params.ai_model}
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
                  key={settings.agent_params.ai_base_url}
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
              <label className="inline">
                rewrite model
                <input
                  className="input" type="text"
                  placeholder={settings.rewrite_model_default || 'same as scoring model'}
                  defaultValue={settings.agent_params.rewrite_model ?? ''}
                  onBlur={(e) => {
                    const v = e.target.value.trim()
                    if (v !== (settings.agent_params.rewrite_model ?? '')) {
                      onSaveSettings({ agent_params: { ...settings.agent_params, rewrite_model: v } })
                    }
                  }}
                />
              </label>
              <label className="inline">
                rewrite base URL
                <input
                  className="input" type="text"
                  placeholder="same as scoring base URL"
                  defaultValue={settings.agent_params.rewrite_base_url ?? ''}
                  onBlur={(e) => {
                    const v = e.target.value.trim()
                    if (v !== (settings.agent_params.rewrite_base_url ?? '')) {
                      onSaveSettings({ agent_params: { ...settings.agent_params, rewrite_base_url: v } })
                    }
                  }}
                />
              </label>
              <label className="inline">
                rewrite tries
                <input
                  className="input input--num" type="number" min="1" max="5"
                  defaultValue={settings.agent_params.rewrite_max_tries ?? 3}
                  onBlur={(e) => {
                    const v = parseInt(e.target.value, 10)
                    if (!Number.isNaN(v) && v !== settings.agent_params.rewrite_max_tries) {
                      onSaveSettings({ agent_params: { ...settings.agent_params, rewrite_max_tries: v } })
                    }
                  }}
                />
              </label>
              <label className="stack-field">
                additional info
                <textarea
                  className="apps-notes"
                  rows={4}
                  placeholder="Work you have done that is not already on the resume"
                  defaultValue={settings.additional_info ?? ''}
                  onBlur={(e) => {
                    const v = e.target.value
                    if (v !== (settings.additional_info ?? '')) {
                      onSaveSettings({ additional_info: v })
                    }
                  }}
                />
              </label>
              <p className="muted small">Defaults come from config.yaml. Values saved here override that file. Model and URL apply from the next run; rate limit and workers apply live. Choosing NVIDIA or OpenAI fills the scoring model and base URL. Empty rewrite model and base URL use the scoring model. API keys stay in the server .env (OPENAI_API_KEY, NVIDIA_API_KEY, or REWRITE_API_KEY for rewrites).</p>
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
              <label className="inline">
                auto-delete after (days)
                <input
                  className="input input--num"
                  type="number" min="0"
                  defaultValue={settings.retention_days ?? 0}
                  title="Delete postings not seen in this many days at the end of each run. 0 = keep everything."
                  onBlur={(e) => {
                    const v = parseInt(e.target.value, 10)
                    if (!Number.isNaN(v) && v >= 0 && v !== settings.retention_days) onSaveSettings({ retention_days: v })
                  }}
                />
              </label>
              <label className="inline">
                delete data older than (days)
                <input
                  className="input input--num"
                  type="number" min="1"
                  value={purgeDays}
                  onChange={(e) => setPurgeDays(e.target.value)}
                  title="Delete jobs first seen more than this many days ago, and run history of the same age. Accepted jobs are kept."
                />
              </label>
              <button className="btn" type="button" onClick={onPurge} disabled={busy}>
                Delete old data
              </button>
              <p className="muted small">
                Blank max = open-ended; auto-delete 0 = keep everything. Changes apply from the next run — the
                pipeline deletes postings not seen for that many days (including auto-rejected ones).
                Delete old data runs immediately and keeps accepted jobs.
              </p>
              {purgeMsg ? <p className="small">{purgeMsg}</p> : null}
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
            {typeof run.summary.pruned === 'number' && run.summary.pruned ? ` · ${run.summary.pruned} old postings deleted` : ''}
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
