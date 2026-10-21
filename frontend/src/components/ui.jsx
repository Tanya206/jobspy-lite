// Shared small presentational components.

export function Spinner({ label }) {
  return (
    <span className="spinner-wrap">
      <span className="spinner" aria-hidden />
      {label ? ` ${label}` : ''}
    </span>
  )
}

export function ProgressBar({ pct }) {
  if (pct == null) return null
  const clamped = Math.max(0, Math.min(100, pct))
  return (
    <div className="progress" role="progressbar" aria-valuenow={clamped} aria-valuemin={0} aria-valuemax={100}>
      <div className="progress__bar" style={{ width: `${clamped}%` }} />
    </div>
  )
}

export function PageHead({ title, lede }) {
  return (
    <header className="page-head">
      <h1>{title}</h1>
      {lede ? <p>{lede}</p> : null}
    </header>
  )
}

export function ErrorBanner({ error }) {
  if (!error) return null
  return (
    <div className="error-banner" role="alert">
      <strong>Something went wrong.</strong>
      <div className="error-banner__msg">{String(error.message || error)}</div>
    </div>
  )
}

export function ScoreGauge({ score }) {
  if (typeof score !== 'number') return <span className="gauge gauge--none">—</span>
  const tier = score >= 75 ? 'high' : score >= 50 ? 'mid' : 'low'
  const r = 22
  const c = 2 * Math.PI * r
  const pct = Math.max(0, Math.min(100, score)) / 100
  return (
    <span className={`gauge gauge--${tier}`} role="img" aria-label={`fit score ${score} of 100`}>
      <svg width="56" height="56" viewBox="0 0 56 56">
        <circle className="gauge__track" cx="28" cy="28" r={r} fill="none" strokeWidth="5" />
        <circle
          className="gauge__fill"
          cx="28" cy="28" r={r} fill="none" strokeWidth="5" strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - pct)} transform="rotate(-90 28 28)"
        />
      </svg>
      <span className="gauge__label">
        <span className="gauge__num">{Math.round(score)}</span>
        <span className="gauge__unit">FIT</span>
      </span>
    </span>
  )
}
