import { useEffect, useMemo, useRef, useState } from 'react'
import {
  createApplication,
  deleteApplication,
  getApplications,
  importApplications,
  updateApplication,
} from '../api.js'
import RewritePanel from './RewritePanel.jsx'
import {
  JOB_STATUSES,
  applicationStats,
  downloadText,
  exportCSV,
  exportJSON,
  formatEmailForClipboard,
  generateEmailMessage,
  generateReferralMessage,
  isOverdue,
  parseCSVImport,
  parseJSONImport,
} from '../tracker.js'

const REFERRALS = ['', 'Yes', 'No']

export default function ApplicationsView() {
  const [apps, setApps] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState('')
  const fileRef = useRef(null)
  const now = useMemo(() => Date.now(), [apps])
  const stats = useMemo(() => applicationStats(apps, now), [apps, now])

  async function load() {
    setError(null)
    try {
      const data = await getApplications()
      setApps(data.applications || [])
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  function patchLocal(id, patch) {
    setApps((rows) => rows.map((row) => (row.id === id ? { ...row, ...patch } : row)))
  }

  async function save(id, patch) {
    setError(null)
    try {
      const updated = await updateApplication(id, patch)
      patchLocal(id, updated)
    } catch (err) {
      setError(err)
      load()
    }
  }

  async function addRow() {
    setBusy(true)
    setError(null)
    try {
      const created = await createApplication({})
      setApps((rows) => [created, ...rows])
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function remove(app) {
    const label = app.company || app.jobRole || 'this row'
    if (!window.confirm(`Delete ${label}?`)) return
    setError(null)
    try {
      await deleteApplication(app.id)
      setApps((rows) => rows.filter((row) => row.id !== app.id))
    } catch (err) {
      setError(err)
    }
  }

  async function copyText(key, value) {
    const text = String(value ?? '')
    if (!text.trim()) return
    setError(null)
    try {
      await writeClipboard(text)
      setCopied(key)
      window.setTimeout(() => setCopied((current) => (current === key ? '' : current)), 1500)
    } catch (err) {
      setError(err instanceof Error ? err : new Error('Could not copy'))
    }
  }

  async function onImport(event) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    if (!window.confirm('Replace the application list with this file?')) return
    setBusy(true)
    setError(null)
    try {
      const text = await file.text()
      const name = file.name.toLowerCase()
      const imported = name.endsWith('.csv') ? parseCSVImport(text) : parseJSONImport(text)
      await importApplications(imported)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section>
      <p className="eyebrow">Applications</p>
      <div className="stat-grid">
        <Stat label="Total" value={stats.total} />
        <Stat label="Interviews" value={stats.interviews} />
        <Stat label="Offers" value={stats.offers} />
        <Stat label="Overdue" value={stats.overdue} accent={stats.overdue > 0} />
      </div>
      <div className="toolbar">
        <button className="btn btn--primary" type="button" onClick={addRow} disabled={busy}>
          Add row
        </button>
        <button className="btn" type="button" onClick={() => downloadText('job-applications.json', exportJSON(apps), 'application/json')}>
          Export JSON
        </button>
        <button className="btn" type="button" onClick={() => downloadText('job-applications.csv', exportCSV(apps), 'text/csv')}>
          Export CSV
        </button>
        <button className="btn" type="button" onClick={() => fileRef.current?.click()} disabled={busy}>
          Import
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".json,.csv,application/json,text/csv"
          hidden
          onChange={onImport}
        />
        <span className="muted small">Accepted jobs are added here as Not Applied.</span>
      </div>
      {error ? (
        <div className="error-banner" role="alert">
          Something went wrong.
          <div className="error-banner__msg">{error.message}</div>
        </div>
      ) : null}
      {loading ? (
        <p className="muted">Loading applications…</p>
      ) : apps.length === 0 ? (
        <div className="empty">
          <h3>No applications yet</h3>
          <p>Accept a job, or add a row to start tracking.</p>
        </div>
      ) : (
        <div className="apps-wrap">
          <table className="apps-table">
            <colgroup>
              <col className="apps-col-company" />
              <col className="apps-col-role" />
              <col className="apps-col-id" />
              <col className="apps-col-link" />
              <col className="apps-col-referral" />
              <col className="apps-col-status" />
              <col className="apps-col-date" />
              <col className="apps-col-notes" />
              <col className="apps-col-messages" />
              <col className="apps-col-resume" />
              <col className="apps-col-delete" />
            </colgroup>
            <thead>
              <tr>
                <th>Company</th>
                <th>Job role</th>
                <th>Job ID</th>
                <th>Link</th>
                <th>Referral</th>
                <th>Status</th>
                <th>Date applied</th>
                <th>Notes</th>
                <th>Messages</th>
                <th className="apps-resume">Résumé</th>
                <th className="apps-delete" />
              </tr>
            </thead>
            <tbody>
              {apps.map((app) => (
                <ApplicationRow
                  key={app.id}
                  app={app}
                  overdue={isOverdue(app, now)}
                  copied={copied}
                  onSave={save}
                  onCopy={copyText}
                  onDelete={remove}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function Stat({ label, value, accent = false }) {
  return (
    <div className={`stat-card ${accent ? 'stat-card--accent' : ''}`}>
      <div className="stat-card__value">{value}</div>
      <div className="stat-card__label">{label}</div>
    </div>
  )
}

async function writeClipboard(text) {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {
      // Permission or a non-secure page. Fall through to the textarea copy.
    }
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.left = '-9999px'
  document.body.appendChild(area)
  area.select()
  const ok = document.execCommand('copy')
  document.body.removeChild(area)
  if (!ok) throw new Error('Could not copy')
  return Promise.resolve()
}

function ApplicationRow({ app, overdue, copied, onSave, onCopy, onDelete }) {
  function commit(field, value) {
    if ((app[field] || '') === value) return
    onSave(app.id, { [field]: value })
  }

  const referralText = generateReferralMessage(app)
  const emailText = formatEmailForClipboard(generateEmailMessage(app))

  return (
    <tr className={overdue ? 'apps-row--overdue' : undefined}>
      <td>
        <input
          className="input"
          aria-label="Company"
          defaultValue={app.company}
          key={`company-${app.id}-${app.company}`}
          onBlur={(event) => commit('company', event.target.value)}
        />
      </td>
      <td>
        <input
          className="input"
          aria-label="Job role"
          defaultValue={app.jobRole}
          key={`role-${app.id}-${app.jobRole}`}
          onBlur={(event) => commit('jobRole', event.target.value)}
        />
      </td>
      <td>
        <input
          className="input"
          aria-label="Job ID"
          defaultValue={app.jobId}
          key={`jobid-${app.id}-${app.jobId}`}
          onBlur={(event) => commit('jobId', event.target.value)}
        />
      </td>
      <td>
        <input
          className="input"
          aria-label="Link"
          defaultValue={app.link}
          key={`link-${app.id}-${app.link}`}
          onBlur={(event) => commit('link', event.target.value)}
        />
      </td>
      <td>
        <select
          className="select"
          aria-label="Referral"
          value={app.referral || ''}
          onChange={(event) => onSave(app.id, { referral: event.target.value })}
        >
          {REFERRALS.map((option) => (
            <option key={option || 'blank'} value={option}>{option || '—'}</option>
          ))}
        </select>
      </td>
      <td>
        <select
          className="select"
          aria-label="Status"
          value={app.status || ''}
          onChange={(event) => onSave(app.id, { status: event.target.value })}
        >
          <option value="">—</option>
          {JOB_STATUSES.map((option) => (
            <option key={option} value={option}>{option}</option>
          ))}
        </select>
        {overdue ? <div className="badge badge--warn">overdue</div> : null}
      </td>
      <td>
        <input
          className="input"
          aria-label="Date applied"
          defaultValue={app.dateApplied}
          key={`applied-${app.id}-${app.dateApplied}`}
          onBlur={(event) => commit('dateApplied', event.target.value)}
        />
      </td>
      <td>
        <textarea
          className="input apps-notes"
          aria-label="Notes"
          defaultValue={app.notes}
          key={`notes-${app.id}-${app.notes}`}
          rows={2}
          onBlur={(event) => commit('notes', event.target.value)}
        />
      </td>
      <td className="apps-actions">
        <button className="btn btn--small" type="button" onClick={() => onCopy(`${app.id}-ref`, referralText)} disabled={!referralText}>
          {copied === `${app.id}-ref` ? 'Copied' : 'Referral'}
        </button>
        <button className="btn btn--small" type="button" onClick={() => onCopy(`${app.id}-mail`, emailText)} disabled={!emailText}>
          {copied === `${app.id}-mail` ? 'Copied' : 'Email'}
        </button>
      </td>
      <td className="apps-resume">
        {app.job_key ? <RewritePanel job={{ job_key: app.job_key, company: app.company }} /> : <span className="muted small">—</span>}
      </td>
      <td className="apps-delete">
        <button className="btn btn--small btn--danger" type="button" onClick={() => onDelete(app)}>
          Delete
        </button>
      </td>
    </tr>
  )
}
