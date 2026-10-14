import { useEffect, useMemo, useState } from 'react'
import { getJobs, setJobStatus } from '../api.js'
import { isSameIstDay } from '../datetime.js'
import JobCard from './JobCard.jsx'
import { ErrorBanner, Spinner } from './ui.jsx'

const FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'new', label: 'New' },
  { key: 'accepted', label: 'Accepted' },
  { key: 'ai-failed', label: 'AI failed' },
]

const REJECTED_PAGE = 200

export default function JobsView() {
  const [filter, setFilter] = useState('all')
  const [order, setOrder] = useState('score')
  const [search, setSearch] = useState('')
  const [jobs, setJobs] = useState([])
  const [rejectedJobs, setRejectedJobs] = useState([])
  const [rejectedTotal, setRejectedTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [busyKey, setBusyKey] = useState(null)
  const [rejectedOpen, setRejectedOpen] = useState(false)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const [data, rejectedData] = await Promise.all([
        getJobs({ order }),
        // Rejected includes both manual rejects and pipeline auto-rejects.
        getJobs({ status: 'rejected', order, limit: REJECTED_PAGE }),
      ])
      setJobs((data.jobs || []).filter((j) => (j.status || 'new') !== 'rejected'))
      setRejectedJobs(rejectedData.jobs || [])
      setRejectedTotal(rejectedData.filtered_total ?? (rejectedData.jobs || []).length)
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [order])

  async function handleStatus(job, status) {
    const prev = { jobs, rejectedJobs, rejectedTotal }
    const wasRejected = (job.status || 'new') === 'rejected'
    setBusyKey(job.job_key)
    // Optimistic update: keep the card in the section that matches its new
    // status; roll back on failure.
    if (wasRejected && status !== 'rejected') {
      setRejectedJobs((list) => list.filter((j) => j.job_key !== job.job_key))
      setJobs((list) => [{ ...job, status }, ...list])
      setRejectedTotal((n) => Math.max(0, n - 1))
    } else if (!wasRejected && status === 'rejected') {
      setJobs((list) => list.filter((j) => j.job_key !== job.job_key))
      setRejectedJobs((list) => [{ ...job, status }, ...list.filter((j) => j.job_key !== job.job_key)])
      setRejectedTotal((n) => n + 1)
    } else {
      const patch = (list) => list.map((j) => (j.job_key === job.job_key ? { ...j, status } : j))
      setJobs(patch)
      setRejectedJobs(patch)
    }
    try {
      await setJobStatus(job.job_key, status)
    } catch (e) {
      setError(e)
      setJobs(prev.jobs)
      setRejectedJobs(prev.rejectedJobs)
      setRejectedTotal(prev.rejectedTotal)
    } finally {
      setBusyKey(null)
    }
  }

  const visible = useMemo(() => {
    let list = jobs
    if (filter === 'ai-failed') {
      list = list.filter((j) => j.score_error && (j.score_attempts || 0) > 0)
    } else if (filter === 'new') {
      // "New" = first seen today (IST calendar day), not merely unreviewed —
      // a backlog of old 'new' rows otherwise drowns the day's harvest.
      list = list.filter(
        (j) => (j.status || 'new') === 'new' && isSameIstDay(j.first_seen_at),
      )
    } else if (filter !== 'all') {
      list = list.filter((j) => (j.status || 'new') === filter)
    }
    return searchFilter(list, search)
  }, [jobs, filter, search])

  const rejectedVisible = useMemo(
    () => searchFilter(rejectedJobs, search),
    [rejectedJobs, search],
  )

  return (
    <section>
      <p className="eyebrow" style={{ marginBottom: 12 }}>Job board</p>
      <div className="toolbar">
        <div className="chips-row">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className={`chip-btn ${filter === f.key ? 'chip-btn--on' : ''}`}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <input
          className="input"
          type="search"
          placeholder="Search title, company, location…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label className="inline">
          sort
          <select className="select" value={order} onChange={(e) => setOrder(e.target.value)}>
            <option value="score">by score</option>
            <option value="recent">by recency</option>
            <option value="title">by title</option>
          </select>
        </label>
        <button className="btn" onClick={load} disabled={loading}>
          {loading ? <Spinner label="Loading…" /> : 'Refresh'}
        </button>
      </div>

      <ErrorBanner error={error} />

      {loading && !jobs.length ? <Spinner label="Loading jobs…" /> : null}

      {!loading && !visible.length ? (
        <div className="empty">
          <h3>No jobs in view</h3>
          <p>Run the pipeline from the Dashboard (or wait for the cron scheduler) to fill the board.</p>
        </div>
      ) : null}

      <div className="grid">
        {visible.map((job) => (
          <JobCard key={job.job_key} job={job} onStatus={handleStatus} busy={busyKey === job.job_key} />
        ))}
      </div>

      {rejectedTotal > 0 || rejectedVisible.length ? (
        <div className="rejected-section">
          <button className="rejected-toggle" onClick={() => setRejectedOpen((v) => !v)}>
            ✕ Rejected ({rejectedTotal}) {rejectedOpen ? '▲' : '▼'}
          </button>
          {rejectedOpen ? (
            <>
              {rejectedTotal > rejectedJobs.length ? (
                <p className="muted small">
                  Showing the first {rejectedJobs.length} of {rejectedTotal} — refine with search or clear old
                  ones via the retention setting on the Overview.
                </p>
              ) : null}
              {rejectedVisible.length ? (
                <div className="grid">
                  {rejectedVisible.map((job) => (
                    <JobCard key={job.job_key} job={job} onStatus={handleStatus} busy={busyKey === job.job_key} />
                  ))}
                </div>
              ) : (
                <p className="muted small">Nothing matches your search.</p>
              )}
            </>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

function searchFilter(list, search) {
  if (!search.trim()) return list
  const needle = search.trim().toLowerCase()
  return list.filter(
    (j) =>
      (j.title || '').toLowerCase().includes(needle) ||
      (j.company || '').toLowerCase().includes(needle) ||
      (j.location || '').toLowerCase().includes(needle),
  )
}
