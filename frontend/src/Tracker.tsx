import { useEffect, useState } from 'react'
import { Download } from 'lucide-react'

type Expense = {
  receipt_id: string
  merchant: string
  document_date: string
  invoice_number: string | null
  due_date: string | null
  paid_on: string | null
  category: string
  currency: string
  amount: string
  overdue: boolean
}
type Total = { currency: string; amount: string; category: string | null }
type Report = { expenses: Expense[]; totals: Total[]; category_totals: Total[]; categories: string[] }

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeZone: 'UTC' })
const formatDate = (value: string) => dateFormat.format(new Date(value + 'T00:00:00Z'))
const today = () => new Date().toISOString().slice(0, 10)

export default function Tracker({ mode }: { mode: 'bills' | 'spending' }) {
  const unpaid = mode === 'bills'
  const [month, setMonth] = useState(() => today().slice(0, 7))
  const [category, setCategory] = useState('')
  const [report, setReport] = useState<Report | null>(null)
  const [loadedQuery, setLoadedQuery] = useState('')
  const [reload, setReload] = useState(0)
  const [categories, setCategories] = useState<string[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [paymentFor, setPaymentFor] = useState<string | null>(null)
  const [paidOn, setPaidOn] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const params = new URLSearchParams({ status: unpaid ? 'unpaid' : 'paid' })
  if (!unpaid && month) params.set('month', month)
  if (category) params.set('category', category)
  const query = params.toString()
  const requestKey = `${query}&reload=${reload}`
  const loading = loadedQuery !== requestKey

  useEffect(() => {
    const controller = new AbortController()
    async function load() {
      try {
        const response = await fetch(`/api/expenses?${query}`, { signal: controller.signal })
        if (!response.ok) throw new Error('Could not load the tracker.')
        const data: Report = await response.json()
        if (!controller.signal.aborted) {
          setReport(data)
          setCategories(data.categories)
        }
      } catch (problem) {
        if (!controller.signal.aborted) {
          setReport(null)
          setError(problem instanceof Error ? problem.message : 'Could not load the tracker.')
        }
      } finally {
        if (!controller.signal.aborted) setLoadedQuery(requestKey)
      }
    }
    void load()
    return () => controller.abort()
  }, [query, requestKey])

  async function update(expense: Expense, action: string, method: string, body: object) {
    if (busy) return
    setBusy(expense.receipt_id)
    setError('')
    setNotice('')
    try {
      const response = await fetch(`/api/expenses/${expense.receipt_id}/${action}`, {
        method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => null)
        throw new Error(typeof payload?.detail === 'string' ? payload.detail : 'Could not update the expense.')
      }
      if (action === 'mark-paid') {
        setPaymentFor(null)
        setNotice(`Payment recorded for ${expense.merchant} on ${formatDate(paidOn)}.`)
      }
      setReload(value => value + 1)
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : 'Could not update the expense.')
    } finally {
      setBusy(null)
    }
  }

  return <main id={mode}>
    <header className="tracker-header">
      <div>
        <h1>{unpaid ? 'Bills to pay' : 'Monthly spending'}</h1>
        <p>{unpaid ? 'Outstanding business bills and their due dates.' : 'Confirmed payments grouped by payment month and currency.'}</p>
      </div>
      <a className="tracker-export" href={`/api/expenses/export.csv?${query}`}><Download size={17} /> Export CSV</a>
    </header>
    <div className="tracker-filters">
      {!unpaid && <label>Payment month<input type="month" value={month} onChange={event => {
        setMonth(event.target.value || today().slice(0, 7))
        setError('')
      }} /></label>}
      <label>Category<select value={category} onChange={event => {
        setCategory(event.target.value)
        setPaymentFor(null)
        setError('')
      }}>
        <option value="">All categories</option>
        {categories.map(name => <option key={name}>{name}</option>)}
      </select></label>
    </div>
    {error && <p className="message error" role="alert">{error}</p>}
    {notice && <p className="message" role="status">{notice}</p>}
    {loading && <p className="tracker-loading" role="status">Loading expenses…</p>}
    {!loading && !report && <button type="button" onClick={() => {
      setError('')
      setReload(value => value + 1)
    }}>Retry</button>}
    {!loading && report && <>
      {report.totals.length > 0 && <section className="tracker-summary" aria-label={unpaid ? 'Outstanding totals' : 'Spending totals'}>
        {report.totals.map(total => <div key={total.currency}>
          <span>{unpaid ? 'Outstanding' : 'Spent'} · {total.currency}</span>
          <strong>{total.currency} {total.amount}</strong>
        </div>)}
      </section>}
      {!unpaid && report.category_totals.length > 0 && <section className="category-summary">
        <h2>Spending by category</h2>
        <ul>{report.category_totals.map(total => <li key={`${total.currency}-${total.category}`}>
          <span>{total.category}</span><strong>{total.currency} {total.amount}</strong>
        </li>)}</ul>
      </section>}
      <section className="library tracker-list" aria-label={unpaid ? 'Unpaid bills' : 'Paid expenses'}>
        <div className="library-heading"><h2>{unpaid ? 'Unpaid bills' : 'Paid expenses'}</h2><span>{report.expenses.length} {report.expenses.length === 1 ? 'record' : 'records'}</span></div>
        {report.expenses.length === 0 ? <div className="empty">{unpaid ? 'No unpaid bills match these filters.' : 'No payments match these filters.'}</div> :
          <ul className="document-list">
            {report.expenses.map(expense => <li className="tracker-row" key={expense.receipt_id}>
              <div className="tracker-merchant">
                <strong>{expense.merchant}</strong>
                {expense.invoice_number && <span className="metadata">Invoice {expense.invoice_number}</span>}
                <span className={`metadata ${expense.overdue ? 'failure' : ''}`}>
                  {unpaid ? expense.due_date ? `${expense.overdue ? 'Overdue' : 'Due'} · ${formatDate(expense.due_date)}` : 'No due date'
                    : expense.paid_on && `Paid ${formatDate(expense.paid_on)}`}
                </span>
              </div>
              <strong className="tracker-amount">{expense.currency} {expense.amount}</strong>
              <select aria-label={`Category for ${expense.merchant}`} value={expense.category} disabled={busy !== null}
                onChange={event => void update(expense, 'category', 'PATCH', { category: event.target.value })}>
                {categories.map(name => <option key={name}>{name}</option>)}
              </select>
              {unpaid && <button type="button" disabled={busy !== null} onClick={() => {
                setPaymentFor(expense.receipt_id)
                setPaidOn(today())
                setError('')
              }}>Mark as paid</button>}
              {paymentFor === expense.receipt_id && <form className="payment-form" onSubmit={event => {
                event.preventDefault()
                void update(expense, 'mark-paid', 'POST', { paid_on: paidOn })
              }}>
                <label>Payment date<input type="date" value={paidOn} max={today()} required
                  onChange={event => setPaidOn(event.target.value)} /></label>
                <button className="primary" disabled={busy !== null}>Record payment</button>
                <button type="button" disabled={busy !== null} onClick={() => setPaymentFor(null)}>Cancel</button>
              </form>}
            </li>)}
          </ul>}
      </section>
    </>}
  </main>
}
