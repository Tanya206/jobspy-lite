import { useEffect, useMemo, useState } from 'react'
import { getRuns, getStats } from '../api.js'
import { parseWhen } from '../datetime.js'
import RunsView from './RunsView.jsx'
import { ErrorBanner, PageHead, Spinner } from './ui.jsx'

const MIX = [
  { key: 'new', label: 'New', color: '#7aa2f7' },
  { key: 'accepted', label: 'Accepted', color: '#3dbe7a' },
  { key: 'manual_rejected', label: 'Rejected', color: '#e05260' },
  { key: 'auto_rejected', label: 'Auto-rejected', color: '#e2a03f' },
]

export default function AnalyticsView() {
  const [stats, setStats] = useState(null)
  const [runs, setRuns] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    Promise.all([getStats(), getRuns(80)])
      .then(([st, data]) => {
        if (!alive) return
        setStats(st)
        setRuns(data.runs || [])
      })
      .catch((e) => alive && setError(e))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  const points = useMemo(() => chartPoints(runs), [runs])
  const mix = MIX.map((slice) => ({ ...slice, value: Number(stats?.[slice.key]) || 0 }))

  return (
    <section className="page">
      <PageHead
        title="Analytics"
        lede="What the board holds, and what each cron run actually brought back."
      />
      <ErrorBanner error={error} />
      {loading ? <Spinner label="Loading analytics…" /> : null}

      <div className="stat-grid">
        <Metric label="Total jobs" value={stats?.total} />
        <Metric label="New" value={stats?.new} />
        <Metric label="Accepted" value={stats?.accepted} />
        <Metric label="Rejected by you" value={stats?.manual_rejected} />
        <Metric label="Auto-rejected" value={stats?.auto_rejected} />
        <Metric label="Average score" value={stats?.avg_score ?? '—'} />
      </div>

      <div className="chart-grid">
        <article className="chart-card">
          <header className="chart-card__head">
            <h2>Jobs per run</h2>
            <p>Scraped postings and how many of them were new.</p>
          </header>
          {points.length ? (
            <AreaChart
              points={points}
              series={[
                { key: 'scraped', label: 'Scraped', color: '#8ab4ff' },
                { key: 'fresh', label: 'New', color: '#3dbe7a' },
              ]}
            />
          ) : (
            <p className="muted small">No runs to chart yet.</p>
          )}
        </article>

        <article className="chart-card">
          <header className="chart-card__head">
            <h2>Board</h2>
            <p>Current jobs by the decision on them.</p>
          </header>
          <Donut slices={mix} />
        </article>
      </div>

      <div className="chart-card chart-card--wide">
        <header className="chart-card__head">
          <h2>Scoring per run</h2>
          <p>How many postings received a score before the run finished.</p>
        </header>
        {points.length ? (
          <AreaChart
            points={points}
            series={[{ key: 'scored', label: 'Scored', color: '#c4b5fd' }]}
          />
        ) : (
          <p className="muted small">No runs to chart yet.</p>
        )}
      </div>

      <h2 className="section-title">Run history</h2>
      {loading ? null : <RunsView embedded runs={runs} />}
    </section>
  )
}

function Metric({ label, value }) {
  return (
    <div className="stat-card">
      <div className="stat-card__value">{value ?? '—'}</div>
      <div className="stat-card__label">{label}</div>
    </div>
  )
}

function chartPoints(runs) {
  return [...runs]
    .reverse()
    .filter((run) => run.started_at)
    .slice(-36)
    .map((run) => ({
      label: shortWhen(run.started_at),
      scraped: Number(run.jobs_scraped) || 0,
      fresh: Number(run.new_jobs) || 0,
      scored: Number(run.scored) || 0,
    }))
}

function shortWhen(value) {
  const date = parseWhen(value)
  if (!date) return ''
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function AreaChart({ points, series }) {
  const w = 640
  const h = 220
  const pad = { l: 36, r: 12, t: 16, b: 28 }
  const max = Math.max(1, ...series.flatMap((s) => points.map((p) => p[s.key])))
  const innerW = w - pad.l - pad.r
  const innerH = h - pad.t - pad.b
  const xAt = (i) => pad.l + (points.length === 1 ? innerW / 2 : (i / (points.length - 1)) * innerW)
  const yAt = (v) => pad.t + innerH - (v / max) * innerH
  const ticks = [max, Math.round(max / 2), 0]
  const labelIdx = [0, Math.floor((points.length - 1) / 2), points.length - 1]
    .filter((v, i, arr) => arr.indexOf(v) === i)

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={series.map((s) => s.label).join(', ')}>
        {ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={pad.l}
              x2={w - pad.r}
              y1={yAt(tick)}
              y2={yAt(tick)}
              className="chart__grid"
            />
            <text x={pad.l - 8} y={yAt(tick) + 4} className="chart__tick" textAnchor="end">
              {tick}
            </text>
          </g>
        ))}
        {series.map((s) => {
          const line = points.map((p, i) => `${xAt(i)},${yAt(p[s.key])}`).join(' ')
          const area = `${pad.l},${yAt(0)} ${line} ${xAt(points.length - 1)},${yAt(0)}`
          return (
            <g key={s.key}>
              <polygon points={area} fill={s.color} opacity="0.16" />
              <polyline points={line} fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
            </g>
          )
        })}
        {labelIdx.map((i) => (
          <text
            key={i}
            x={xAt(i)}
            y={h - 8}
            className="chart__tick"
            textAnchor={i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle'}
          >
            {points[i].label}
          </text>
        ))}
      </svg>
      <div className="chart__legend">
        {series.map((s) => (
          <span key={s.key}>
            <i style={{ background: s.color }} />
            {s.label}
            <b>{points[points.length - 1][s.key]}</b>
          </span>
        ))}
      </div>
    </div>
  )
}

function Donut({ slices }) {
  const total = slices.reduce((sum, slice) => sum + slice.value, 0)
  const r = 54
  const c = 2 * Math.PI * r
  let offset = 0
  return (
    <div className="donut">
      <svg viewBox="0 0 140 140" role="img" aria-label="Job status mix">
        <circle cx="70" cy="70" r={r} className="donut__track" />
        {total
          ? slices.map((slice) => {
              const len = (slice.value / total) * c
              const dash = `${len} ${c - len}`
              const el = (
                <circle
                  key={slice.key}
                  cx="70"
                  cy="70"
                  r={r}
                  fill="none"
                  stroke={slice.color}
                  strokeWidth="14"
                  strokeDasharray={dash}
                  strokeDashoffset={-offset}
                  transform="rotate(-90 70 70)"
                />
              )
              offset += len
              return el
            })
          : null}
        <text x="70" y="66" className="donut__total" textAnchor="middle">
          {total}
        </text>
        <text x="70" y="84" className="chart__tick" textAnchor="middle">
          jobs
        </text>
      </svg>
      <ul className="donut__legend">
        {slices.map((slice) => (
          <li key={slice.key}>
            <i style={{ background: slice.color }} />
            <span>{slice.label}</span>
            <b>{slice.value}</b>
          </li>
        ))}
      </ul>
    </div>
  )
}
