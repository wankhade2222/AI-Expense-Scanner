import { useEffect, useState } from 'react'

type Flag = {
  field: string
  code: string
  severity: 'blocking' | 'warning'
  reason: string
}

type Review = {
  version: { extraction_id: string; revision: number }
  fields: Record<string, string | null> & {
    line_items: Array<Record<string, string | null>>
    sources: Array<{ field: string; page: number; box: number[] | null }>
  }
  validation: { flags: Flag[] }
  confirmed: boolean
  category: string
  categories: string[]
}

const labels: Record<string, string> = {
  merchant: 'Merchant', document_type: 'Document type', document_date: 'Document date',
  invoice_number: 'Invoice number', due_date: 'Due date', payment_status: 'Payment status',
  payment_date: 'Payment date', currency: 'Currency', gstin: 'GSTIN', subtotal: 'Subtotal',
  discount: 'Discount', tax: 'Tax', fees: 'Fees', rounding: 'Rounding', total: 'Printed total',
}
const options: Record<string, string[]> = {
  payment_status: ['paid', 'unpaid', 'unknown'],
  document_type: ['receipt', 'invoice'],
}
const lineLabels = { description: 'description', qty: 'quantity', unit_price: 'unit price', amount: 'amount' }

export default function ReviewCard({ receiptId }: { receiptId: string }) {
  const [review, setReview] = useState<Review | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [category, setCategory] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [reload, setReload] = useState(0)
  const base = `/api/receipts/${receiptId}`

  useEffect(() => {
    const controller = new AbortController()
    async function load() {
      try {
        const response = await fetch(`${base}/review`, { signal: controller.signal })
        if (response.status === 404) return
        if (!response.ok) throw new Error('Could not load the review.')
        const next = await response.json()
        if (!controller.signal.aborted) setReview(next)
      } catch (problem) {
        if (!controller.signal.aborted) setError(String(problem instanceof Error ? problem.message : problem))
      } finally {
        if (!controller.signal.aborted) setLoaded(true)
      }
    }
    void load()
    return () => controller.abort()
  }, [base, reload])

  async function change(path: string, method: string, data: object) {
    if (!review || busy) return
    setBusy(true)
    setError('')
    try {
      const response = await fetch(`${base}${path}`, {
        method, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...review.version, ...data }),
      })
      if (!response.ok) {
        const body = await response.json()
        throw new Error(typeof body.detail === 'string' ? body.detail : 'Could not save the review.')
      }
      setReview(await response.json())
      setEditing(null)
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : 'Could not save the review.')
    } finally {
      setBusy(false)
    }
  }

  function reloadReview() {
    setError('')
    setReview(null)
    setEditing(null)
    setCategory(null)
    setLoaded(false)
    setReload(previous => previous + 1)
  }

  const feedback = error && <div role="alert" className="review-error">
    <span>{error}</span> <button type="button" onClick={reloadReview}>Reload review</button>
  </div>
  if (!loaded) return <p role="status">Loading review…</p>
  if (!review) return feedback || <p className="metadata">No extraction is available to review.</p>

  const flags = review.validation.flags
  const rows = [
    ...Object.entries(labels).map(([field, label]) => ({ field, label, value: review.fields[field] })),
    ...review.fields.line_items.flatMap((item, index) => Object.entries(lineLabels).map(([key, label]) => ({
      field: `line_items.${index}.${key}`, label: `Item ${index + 1} ${label}`, value: item[key],
    }))),
  ]
  function priority(field: string) {
    const matches = flags.filter(flag => flag.field === field)
    return matches.some(flag => flag.severity === 'blocking') ? 0 : matches.length ? 1 : 2
  }
  const source = review.fields.sources.find(location => location.field === editing && location.box)
    ?? review.fields.sources.find(location => location.field === editing)

  return <section className="review-card" aria-label="Review extracted fields">
    {feedback}
    {flags.filter(flag => flag.field === 'document').map(flag => <p className="review-flag" key={flag.code}>{flag.reason}</p>)}
    <div className="review-fields">
      {rows.sort((a, b) => priority(a.field) - priority(b.field)).map(({ field, label, value }) => (
        <div className="review-row" key={field}>
          <div className="review-row-value">
            <strong>{label}</strong>
            <span>{value ?? '—'}</span>
          </div>
          {flags.filter(flag => flag.field === field).map(flag =>
            <p className="review-flag" key={flag.code}>{flag.severity === 'blocking' ? 'Required · ' : ''}{flag.reason}</p>,
          )}
          {editing === field ? <form className="review-edit" onSubmit={event => {
            event.preventDefault()
            void change(`/fields/${field}`, 'PATCH', { value: draft || null })
          }}>
            {options[field] ? <select aria-label={label} value={draft} autoFocus onChange={event => setDraft(event.target.value)}>
              {field === 'document_type' && <option value="">Choose type</option>}
              {options[field].map(option => <option key={option}>{option}</option>)}
            </select> : <input aria-label={label} value={draft} maxLength={1000} autoFocus
              onChange={event => setDraft(event.target.value)} />}
            <button type="submit" className="primary" disabled={busy}>Save</button>
            <button type="button" disabled={busy} onClick={() => setEditing(null)}>Cancel</button>
            {source && <figure className="review-source">
              <img alt={`Source for ${label}, page ${source.page}`}
                src={`${base}/review/source?${new URLSearchParams({ field, extraction_id: review.version.extraction_id })}`} />
              <figcaption>{source.box ? 'Source crop' : 'Source page'} · Page {source.page}</figcaption>
            </figure>}
          </form> : !review.confirmed &&
            <button type="button" aria-label={`Edit ${label}`} disabled={busy || editing !== null}
              onClick={() => {
                setEditing(field)
                setDraft(value ?? '')
              }}>Edit</button>}
        </div>
      ))}
    </div>
    <div className="review-footer">{review.confirmed ? <p className="review-confirmed" role="status">Confirmed · {review.category}</p> : <>
      <label className="review-category">Category
        <select value={category ?? review.category} onChange={event => setCategory(event.target.value)} disabled={busy}>
          {review.categories.map(name => <option key={name}>{name}</option>)}
        </select>
      </label>
      <button type="button" className="primary review-confirm" disabled={busy || editing !== null || flags.some(flag => flag.severity === 'blocking')}
        onClick={() => void change('/approve', 'POST', { category: category ?? review.category })}>Confirm document</button></>}</div>
  </section>
}
