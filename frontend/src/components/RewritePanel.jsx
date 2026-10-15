import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { listRewrites, rewriteDocxUrl, startRewrite } from '../api.js'

function rewriteFileName(company) {
  const cleaned = String(company || '').replace(/[^A-Za-z0-9]+/g, '_').replace(/^_|_$/g, '')
  return `Rohil_Chaturvedi_${cleaned || 'Company'}.docx`
}

export default function RewritePanel({ job, startToken = 0 }) {
  const [attempt, setAttempt] = useState(null)
  const [error, setError] = useState('')
  const [open, setOpen] = useState(false)
  const changesRef = useRef(null)
  const running = attempt?.status === 'running'

  async function refresh() {
    const body = await listRewrites(job.job_key)
    const latest = (body.rewrites || [])[0] || null
    setAttempt(latest)
    return latest
  }

  useEffect(() => {
    let cancelled = false
    listRewrites(job.job_key)
      .then((body) => {
        if (!cancelled) setAttempt((body.rewrites || [])[0] || null)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || 'Could not load the rewrite')
      })
    return () => {
      cancelled = true
    }
  }, [job.job_key])

  useEffect(() => {
    if (!running) return undefined
    const timer = setInterval(() => {
      refresh().catch((err) => setError(err.message || 'Could not load the rewrite'))
    }, 3000)
    return () => clearInterval(timer)
  }, [running, job.job_key])

  useEffect(() => {
    if (startToken > 0) run()
    // startToken is the only trigger for the automatic run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startToken])

  async function run() {
    setError('')
    setAttempt((current) => ({ ...(current || {}), status: 'running', docx_ready: false, winning_patches: [] }))
    try {
      const created = await startRewrite(job.job_key, {})
      setAttempt(created)
    } catch (err) {
      setError(err.message || 'Rewrite failed')
      refresh().catch(() => {})
    }
  }

  const ready = Boolean(attempt?.docx_ready)
  const patches = attempt?.winning_patches || []

  return (
    <div className="rewrite-actions">
      {ready ? (
        <a
          className="btn btn--small"
          href={rewriteDocxUrl(job.job_key, attempt.id)}
          download={rewriteFileName(job.company)}
        >
          Download résumé
        </a>
      ) : (
        <button className="btn btn--small" type="button" disabled title="Available when the rewrite finishes">
          Download résumé
        </button>
      )}
      <button className="btn btn--small" type="button" onClick={run} disabled={running}>
        {running ? 'Rewriting…' : 'Rerun rewrite'}
      </button>
      <button
        ref={changesRef}
        className="btn btn--small"
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        Changes
      </button>
      {error ? <span className="muted small">{error}</span> : null}
      {open ? (
        <ChangesPopover anchor={changesRef} onClose={() => setOpen(false)} running={running} patches={patches} error={attempt?.error} />
      ) : null}
    </div>
  )
}

function ChangesPopover({ anchor, onClose, running, patches, error }) {
  const [box, setBox] = useState(null)
  const popRef = useRef(null)

  useEffect(() => {
    function place() {
      const node = anchor.current
      if (!node) return
      const rect = node.getBoundingClientRect()
      const width = 320
      const height = 220
      const spaceBelow = window.innerHeight - rect.bottom
      const top = spaceBelow < 180
        ? Math.max(8, rect.top - height - 6)
        : rect.bottom + 6
      setBox({
        top,
        left: Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8)),
      })
    }
    place()
    function onPointer(event) {
      const pop = popRef.current
      const button = anchor.current
      if (pop && pop.contains(event.target)) return
      if (button && button.contains(event.target)) return
      onClose()
    }
    function onKey(event) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('resize', place)
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('resize', place)
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [anchor, onClose])

  if (!box) return null
  return createPortal(
    <div ref={popRef} className="rewrite-pop" style={{ top: box.top, left: box.left }} role="dialog" aria-label="Resume changes">
      {running ? <p className="muted small">Rewriting this résumé…</p> : null}
      {error ? <p className="muted small">{error}</p> : null}
      {!running && !patches.length ? <p className="muted small">No wording changes.</p> : null}
      {patches.map((patch, index) => (
        <div className="rewrite-pop__row" key={`${index}-${patch.original}`}>
          <p className="rewrite-pop__from">{patch.original}</p>
          <p>{patch.replacement}</p>
        </div>
      ))}
    </div>,
    document.body,
  )
}
