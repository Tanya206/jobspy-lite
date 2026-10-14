/** Spreadsheet helpers for the Applications tab. */

export const JOB_STATUSES = [
  'Wishlist',
  'Not Applied',
  'Applied',
  'Interview',
  'Offer',
  'Rejected',
]

const APPLIED_STATUSES = ['Applied', 'Rejected', 'Interview', 'Offer']
const MS_PER_DAY = 24 * 60 * 60 * 1000
export const OVERDUE_DAYS = 2

const CSV_HEADERS = [
  'id',
  'company',
  'jobRole',
  'jobId',
  'link',
  'referral',
  'status',
  'dateApplied',
  'notes',
  'dateAdded',
  'job_key',
]

export function isPendingStatus(status) {
  if (!status) return true
  return !APPLIED_STATUSES.includes(status)
}

export function isOverdue(app, now = Date.now()) {
  if (!isPendingStatus(app.status)) return false
  const added = new Date(app.dateAdded).getTime()
  if (Number.isNaN(added)) return false
  return Math.floor((now - added) / MS_PER_DAY) > OVERDUE_DAYS
}

export function applicationStats(apps, now = Date.now()) {
  return {
    total: apps.length,
    interviews: apps.filter((app) => app.status === 'Interview').length,
    offers: apps.filter((app) => app.status === 'Offer').length,
    overdue: apps.filter((app) => isOverdue(app, now)).length,
  }
}

/** Short referral note. Blank company yields a blank message. */
export function generateReferralMessage(app) {
  if (!String(app.company || '').trim()) return ''
  const nl = '\n'
  let msg =
    `Hi ,${nl}` +
    `Hope you're doing well! I'm a GenAI Specialist at Infosys, building production agentic systems with LangGraph and FastAPI. I saw the ${app.jobRole || ''} opening at ${app.company} and think my background could be a strong fit — would you be open to referring me before I apply?${nl}${nl}`
  if (String(app.jobId || '').trim()) msg += `Job ID: ${app.jobId}${nl}`
  if (String(app.link || '').trim()) msg += `Job Link : ${app.link}${nl}${nl}`
  else if (String(app.jobId || '').trim()) msg += nl
  msg +=
    `I've attached my resume for context. No worries at all if you're not able to — really appreciate you considering it.${nl}` +
    `Thanks,${nl}` +
    `Rohil`
  return msg
}

/** Longer email (subject + body), distinct from the short referral note. */
export function generateEmailMessage(app) {
  if (!String(app.company || '').trim()) return { subject: '', body: '' }
  const role = String(app.jobRole || '').trim() || 'open role'
  const subject = `Referral request — ${role} at ${app.company}`
  const lines = [
    'Hi,',
    '',
    "I hope this email finds you well. My name is Rohil, and I'm a GenAI Specialist at Infosys focused on production agentic systems with LangGraph and FastAPI.",
    '',
    `I'm writing because I came across the ${role} position at ${app.company} and believe my background would be a strong fit. Would you be open to referring me for this role before I submit my application?`,
    '',
  ]
  if (String(app.jobId || '').trim() || String(app.link || '').trim()) {
    lines.push('Role details:')
    if (String(app.jobId || '').trim()) lines.push(`• Job ID: ${app.jobId}`)
    if (String(app.link || '').trim()) lines.push(`• Job Link: ${app.link}`)
    lines.push('')
  }
  lines.push(
    "I've attached my resume for additional context. Please don't feel obligated if you're unable to help — I truly appreciate you taking the time to consider this.",
    '',
    'Thank you,',
    'Rohil',
  )
  return { subject, body: lines.join('\n') }
}

export function formatEmailForClipboard(email) {
  if (!email.subject && !email.body) return ''
  return `Subject: ${email.subject}\n\n${email.body}`
}

function csvEscape(value) {
  const text = String(value ?? '')
  if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`
  return text
}

export function exportJSON(apps) {
  return JSON.stringify(apps, null, 2)
}

export function exportCSV(apps) {
  const rows = apps.map((app) => CSV_HEADERS.map((header) => csvEscape(app[header] ?? '')).join(','))
  return [CSV_HEADERS.join(','), ...rows].join('\n')
}

export function parseJSONImport(text) {
  const parsed = JSON.parse(text)
  if (!Array.isArray(parsed)) throw new Error('JSON must be an array of applications')
  return parsed
}

export function parseCSVImport(text) {
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/).filter((line) => line.trim())
  if (lines.length < 2) return []
  const headers = parseCSVLine(lines[0]).map((header) => header.trim())
  return lines.slice(1).map((line) => {
    const cells = parseCSVLine(line)
    const row = {}
    headers.forEach((header, index) => {
      row[header] = cells[index] ?? ''
    })
    return row
  })
}

function parseCSVLine(line) {
  const result = []
  let current = ''
  let inQuotes = false
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i]
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') {
          current += '"'
          i += 1
        } else {
          inQuotes = false
        }
      } else {
        current += ch
      }
    } else if (ch === '"') {
      inQuotes = true
    } else if (ch === ',') {
      result.push(current)
      current = ''
    } else {
      current += ch
    }
  }
  result.push(current)
  return result
}

export function downloadText(filename, text, type) {
  const blob = new Blob([text], { type })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}
