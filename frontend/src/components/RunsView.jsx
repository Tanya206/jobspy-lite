import { useEffect, useState } from 'react'
import { getRuns } from '../api.js'
import { formatIst, secondsBetween } from '../datetime.js'
import { ErrorBanner, Spinner } from './ui.jsx'

export default function RunsView({ embedded = false, runs: externalRuns }) {
  const controlled = externalRuns !== undefined
  const [runs, setRuns] = useState(externalRuns || [])
  const [loading, setLoading] = useState(!controlled)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (controlled) {
      setRuns(externalRuns)
      setLoading(false)
      return
    }
    setLoading(true)
    getRuns(100)
      .then((data) => setRuns(data.runs || []))
      .catch(setError)
      .finally(() => setLoading(false))
  }, [controlled, externalRuns])

  return (
    <section>
      {embedded ? null : <p className="eyebrow" style={{ marginBottom: 12 }}>Pipeline runs</p>}
      <ErrorBanner error={error} />
      {loading ? <Spinner label="Loading runs…" /> : null}
      {!loading && !runs.length ? (
        <div className="empty">
          <h3>No runs yet</h3>
          <p>Start one from Settings, or wait for the scheduler.</p>
        </div>
      ) : null}
      {runs.length ? (
        <div className="runs-scroll">
        <table className="runs-table">
          <thead>
            <tr>
              <th>started</th>
              <th>trigger</th>
              <th>status</th>
              <th>scraped</th>
              <th>new</th>
              <th>scored</th>
              <th>alerts</th>
              <th>took</th>
              <th>error</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id} className={run.status === 'failed' || run.status === 'degraded' ? 'runs-table__row--bad' : ''}>
                <td>{formatIst(run.started_at)}</td>
                <td><span className="badge">{run.trigger}</span></td>
                <td>
                  <span className={`badge ${run.status === 'completed' ? 'badge--ok' : run.status === 'failed' ? 'badge--no' : run.status === 'degraded' ? 'badge--warn' : ''}`}>
                    {run.status}
                  </span>
                </td>
                <td>{run.jobs_scraped ?? '—'}</td>
                <td>{run.new_jobs ?? '—'}</td>
                <td>{run.scored ?? '—'}</td>
                <td>{run.alerts_sent ?? '—'}</td>
                <td>{secondsBetween(run.started_at, run.finished_at)
                  ? `${secondsBetween(run.started_at, run.finished_at)}s`
                  : '—'}</td>
                <td className="runs-table__error">
                  {run.error || (Array.isArray(run.scrape_errors) && run.scrape_errors.length
                    ? run.scrape_errors.join('; ')
                    : '')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : null}
    </section>
  )
}
