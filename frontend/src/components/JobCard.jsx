import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { formatIstDate } from '../datetime.js'
import { renderMarkdown } from '../markdown.js'
import { ScoreGauge } from './ui.jsx'

function salaryText(job) {
  const { min_amount, max_amount, currency, interval } = job
  if (!min_amount && !max_amount) return null
  const fmt = (n) => (typeof n === 'number' ? n.toLocaleString() : n)
  const range =
    min_amount && max_amount ? `${fmt(min_amount)}–${fmt(max_amount)}` : fmt(min_amount || max_amount)
  return `${currency || ''} ${range}${interval ? ` / ${interval}` : ''}`.trim()
}

function requiredYearsText(job) {
  const { required_years_min, required_years_max } = job
  if (required_years_min == null && required_years_max == null) return null
  if (required_years_max == null) return `needs ${required_years_min}+ yrs`
  if (required_years_min === required_years_max) {
    return `needs ${required_years_min} yr${required_years_min === 1 ? '' : 's'}`
  }
  return `needs ${required_years_min}–${required_years_max} yrs`
}

function verdictBadgeClass(verdict) {
  const v = (verdict || '').toLowerCase()
  if (v.includes('strong')) return 'badge badge--ok'
  if (v.includes('good') || v.includes('moderate')) return 'badge badge--warn'
  if (v.includes('weak') || v.includes('poor')) return 'badge badge--no'
  return 'badge'
}

function detailLabel(key) {
  return key.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function detailValue(value) {
  if (value == null || value === '') return '—'
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—'
  if (typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'string' && /_at$/.test(value)) {
    const asDate = new Date(value)
    if (!Number.isNaN(asDate.getTime())) return formatIstDate(value)
  }
  return String(value)
}

function JobDetails({ job, onClose, onStatus }) {
  useEffect(() => {
    function onKeyDown(event) {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const aiKeys = new Set([
    'score', 'verdict', 'required_years', 'experience_match',
    'matched_skills', 'missing_skills', 'reasoning',
  ])
  const entries = Object.entries(job).filter(
    ([key]) => key !== 'description' && !aiKeys.has(key),
  )
  const hasAiDetails = Object.keys(job).some((key) => aiKeys.has(key))

  return (
    <div
      className="details-modal"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <section className="details-modal__window" role="dialog" aria-modal="true" aria-labelledby="job-details-title">
        <div className="details-modal__head">
          <div>
            <p className="eyebrow">Posting details</p>
            <h2 id="job-details-title">{job.title || 'Untitled role'}</h2>
            <p className="muted">{job.company || 'Unknown company'}{job.location ? ` · ${job.location}` : ''}</p>
          </div>
          <button className="icon-btn" type="button" onClick={onClose} aria-label="Close details">×</button>
        </div>
        <div className="details-modal__body">
          {hasAiDetails ? (
            <div className="details-ai">
              <h3>AI assessment</h3>
              <div className="details-ai__summary">
                {job.score != null ? <strong className="details-ai__score">{job.score}<small>/100 fit</small></strong> : null}
                {job.verdict && job.verdict !== 'unknown' ? (
                  <span className={verdictBadgeClass(job.verdict)}>{job.verdict} fit</span>
                ) : null}
                {job.required_years_min != null || job.required_years_max != null ? (
                  <span className="badge badge--accent">{requiredYearsText(job)}</span>
                ) : null}
                {job.experience_match != null ? (
                  <span className={job.experience_match ? 'badge badge--ok' : 'badge badge--no'}>
                    {job.experience_match ? '✓ matches experience' : '✕ outside experience window'}
                  </span>
                ) : null}
              </div>
              {Array.isArray(job.matched_skills) && job.matched_skills.length ? (
                <div className="details-ai__skills"><b>Matched skills</b>{job.matched_skills.map((skill, index) => <span className="chip chip--ok" key={`matched-${index}`}>{skill}</span>)}</div>
              ) : null}
              {Array.isArray(job.missing_skills) && job.missing_skills.length ? (
                <div className="details-ai__skills"><b>Missing skills</b>{job.missing_skills.map((skill, index) => <span className="chip chip--miss" key={`missing-${index}`}>{skill}</span>)}</div>
              ) : null}
              {job.reasoning ? <p className="details-ai__reasoning">{job.reasoning}</p> : null}
            </div>
          ) : null}
          {job.description ? (
            <div className="details-modal__description">
              <h3>Description</h3>
              {/* Scraped postings are markdown; render rich text like the
                  original dashboard (input is HTML-escaped in markdown.js). */}
              <div className="desc desc--rich" dangerouslySetInnerHTML={{ __html: renderMarkdown(job.description) }} />
            </div>
          ) : null}
          <dl className="details-grid">
            {entries.map(([key, value]) => (
              <div className="details-grid__item" key={key}>
                <dt>{detailLabel(key)}</dt>
                <dd>{detailValue(value)}</dd>
              </div>
            ))}
          </dl>
        </div>
        <div className="details-modal__foot">
          {job.job_url ? (
            <a className="btn btn--primary" href={job.job_url} target="_blank" rel="noreferrer">
              Open original ↗
            </a>
          ) : null}
          <button className="btn" type="button" onClick={onClose}>Close</button>
        </div>
      </section>
    </div>
  )
}

export default function JobCard({ job, onStatus, busy }) {
  const [detailsJob, setDetailsJob] = useState(null)
  const salary = salaryText(job)
  const matched = job.matched_skills || []
  const missing = job.missing_skills || []
  const status = job.status || 'new'
  const hasFit = typeof job.score === 'number' || job.verdict || job.reasoning

  function openFromTile(event) {
    if (event.target.closest('button, a, input, select, textarea')) return
    setDetailsJob(job)
  }

  return (
    <article className={`card card--${status}`} onClick={openFromTile}>
      <div className="card__head">
        <div>
          <h3 className="card__title">{job.title || 'Untitled role'}</h3>
          <div className="card__company">
            {job.company || 'Unknown company'}
            {job.location ? <span className="card__loc"> · {job.location}</span> : null}
          </div>
        </div>
        <div className="card__actions">
          <button
            className={`btn btn--small ${status === 'accepted' ? 'btn--ok' : ''}`}
            disabled={busy}
            onClick={() => onStatus(job, 'accepted')}
            title="Accept this job"
          >
            {status === 'accepted' ? 'Accepted ✓' : 'Accept'}
          </button>
          <button
            className={`btn btn--small ${status === 'rejected' ? 'btn--danger' : ''}`}
            disabled={busy}
            onClick={() => onStatus(job, 'rejected')}
            title="Reject this job"
          >
            {status === 'rejected' ? 'Rejected ✕' : 'Reject'}
          </button>
        </div>
      </div>

      <div className="badges">
        {job.auto_rejected ? (
          <span className="badge badge--warn" title="Rejected by the pipeline because required experience is outside your window">
            auto · outside experience window
          </span>
        ) : null}
        {job.experience_match != null ? (
          <span className={job.experience_match ? 'badge badge--ok' : 'badge badge--no'}>
            {job.experience_match ? '✓ matches experience' : '✕ outside window'}
          </span>
        ) : null}
        {requiredYearsText(job) ? <span className="badge badge--accent">{requiredYearsText(job)}</span> : null}
        {job.site ? <span className="badge">{job.site}</span> : null}
        {job.job_type ? <span className="badge">{job.job_type}</span> : null}
        {job.is_remote ? <span className="badge badge--accent">remote</span> : null}
        {salary ? <span className="badge badge--muted">{salary}</span> : null}
        {job.date_posted ? <span className="badge badge--muted">{String(job.date_posted).slice(0, 10)}</span> : null}
        {job.notified ? <span className="badge badge--muted" title="Telegram alert sent">alerted</span> : null}
        {job.score_error && (job.score_attempts || 0) > 0 ? (
          <span
            className={`badge ${(job.score_attempts || 0) >= 3 ? 'badge--no' : 'badge--warn'}`}
            title={`AI scoring failed ${job.score_attempts}x: ${job.score_error}`}
          >
            {(job.score_attempts || 0) >= 3
              ? 'AI failed — retries exhausted'
              : `AI retry ${job.score_attempts}/3 pending`}
          </span>
        ) : null}
      </div>

      {hasFit ? (
        <div className="fit">
          <ScoreGauge score={typeof job.score === 'number' ? job.score : null} />
          <div className="fit__body">
            <div className="fit__verdict">
              {job.verdict && job.verdict !== 'unknown' ? (
                <span className={verdictBadgeClass(job.verdict)}>{job.verdict} fit</span>
              ) : null}
              {matched.length || missing.length ? (
                <span className="badge badge--muted">
                  {matched.length}✓ · {missing.length}✕ skills
                </span>
              ) : null}
            </div>
            {matched.length || missing.length ? (
              <div className="chips">
                {matched.slice(0, 10).map((s, i) => (
                  <span key={`m${i}`} className="chip chip--ok">✓ {s}</span>
                ))}
                {missing.slice(0, 10).map((s, i) => (
                  <span key={`x${i}`} className="chip chip--miss">✕ {s}</span>
                ))}
              </div>
            ) : null}
            {job.reasoning ? <p className="fit__why">{job.reasoning}</p> : null}
          </div>
        </div>
      ) : null}

      {job.description ? (
        <div className="card__desc">
          <div
            className="desc desc--rich"
            dangerouslySetInnerHTML={{ __html: renderMarkdown(job.description) }}
          />
        </div>
      ) : null}

      <div className="card__foot">
        <button className="link-btn" type="button" onClick={() => setDetailsJob(job)}>
          View details
        </button>
        {job.first_seen_at ? (
          <span className="card__id">seen {formatIstDate(job.first_seen_at)}</span>
        ) : null}
      </div>

      {detailsJob
        ? createPortal(<JobDetails job={detailsJob} onClose={() => setDetailsJob(null)} />, document.body)
        : null}
    </article>
  )
}
