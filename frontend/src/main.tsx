import { useCallback, useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { AlertCircle, Camera, CheckCircle2, Clock3, FileText, FolderOpen, LoaderCircle, ReceiptText, RotateCw, UploadCloud, X } from 'lucide-react'
import './styles.css'

type Receipt = {
  id: string
  filename: string
  content_type: string
  size: number
  status: 'queued' | 'processing' | 'ready' | 'failed'
  created_at: string
  page_count: number | null
  preview_url: string | null
  error: string | null
}

type Extraction = {
  fields: {
    merchant: string | null
    document_date: string | null
    currency: string | null
    total: string | null
    payment_status: 'paid' | 'unpaid' | 'unknown'
  }
  validation?: {
    flags: Array<{
      field: string
      code: string
      severity: 'blocking' | 'warning'
      reason: string
      source: { page: number } | null
    }>
  }
}

const statusLabels = { queued: 'Queued', processing: 'Processing', ready: 'Ready', failed: 'Failed' }
const dateFormat = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
const supported = 'image/jpeg,image/png,image/webp,application/pdf,.pdf'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/receipts${path}`, options)
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(typeof body?.detail === 'string' ? body.detail : 'Could not reach the scanner. Try again.')
  }
  return response.json()
}

function describeError(error: unknown) {
  return error instanceof Error ? error.message : 'Something went wrong. Try again.'
}

function Status({ status }: { status: Receipt['status'] }) {
  const Icon = { queued: Clock3, processing: LoaderCircle, ready: CheckCircle2, failed: AlertCircle }[status]
  return <span className={`status ${status}`}><Icon size={14} />{statusLabels[status]}</span>
}

function App() {
  const [receipts, setReceipts] = useState<Receipt[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState('')
  const [listError, setListError] = useState('')
  const [notice, setNotice] = useState('')
  const [selected, setSelected] = useState<Receipt | null>(null)
  const [extraction, setExtraction] = useState<Extraction | null>(null)
  const [retrying, setRetrying] = useState<string | null>(null)
  const picker = useRef<HTMLInputElement>(null)
  const camera = useRef<HTMLInputElement>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  const processing = receipts.some(({ status }) => status === 'queued' || status === 'processing')

  const refresh = useCallback(async () => {
    try {
      setReceipts(await request<Receipt[]>(''))
      setListError('')
    } catch (problem) {
      setListError(describeError(problem))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  useEffect(() => {
    if (!processing) return
    const timer = window.setInterval(() => { void refresh() }, 1800)
    return () => window.clearInterval(timer)
  }, [processing, refresh])

  useEffect(() => {
    if (selected) dialog.current?.showModal()
    else dialog.current?.close()
  }, [selected])

  useEffect(() => {
    setExtraction(null)
    if (!selected) return
    const controller = new AbortController()
    void request<Extraction>(`/${selected.id}/extraction`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setExtraction(result) })
      .catch(() => {})
    return () => controller.abort()
  }, [selected])

  function remember(receipt: Receipt) {
    setReceipts(previous => [receipt, ...previous.filter(row => row.id !== receipt.id)]
      .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)))
  }

  async function upload(file?: File) {
    if (!file || busy) return
    setError('')
    setNotice('')
    if (file.size > 20 * 1024 * 1024) {
      setError('Choose a file up to 20 MB.')
      return
    }
    const form = new FormData()
    form.append('file', file)
    setBusy(true)
    try {
      const receipt = await request<Receipt>('', { method: 'POST', body: form })
      remember(receipt)
      setNotice(`${receipt.filename} is in your documents.`)
    } catch (problem) {
      setError(describeError(problem))
    } finally {
      setBusy(false)
    }
  }

  async function retry(receipt: Receipt) {
    setRetrying(receipt.id)
    setError('')
    setNotice('')
    try {
      remember(await request<Receipt>(`/${receipt.id}/retry`, { method: 'POST' }))
    } catch (problem) {
      setError(describeError(problem))
    } finally {
      setRetrying(null)
    }
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand"><span className="brand-icon"><ReceiptText size={22} /></span><span>Expense Scanner</span></div>
        <nav aria-label="Workspace">
          <a href="#documents" aria-current="page"><FolderOpen size={19} /> Documents</a>
        </nav>
      </aside>
      <main id="documents">
        <header>
          <h1>Documents</h1>
          <p>Upload and manage your business receipts and invoices.</p>
        </header>
        <section
          className={`dropzone ${dragging ? 'dragging' : ''}`}
          aria-label="Upload a document"
          onDragOver={event => { event.preventDefault(); setDragging(true) }}
          onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false) }}
          onDrop={event => {
            event.preventDefault()
            setDragging(false)
            if (event.dataTransfer.files.length > 1) {
              setError('Upload one receipt or invoice at a time.')
              setNotice('')
              return
            }
            void upload(event.dataTransfer.files[0])
          }}
        >
          <div className="upload-icon"><UploadCloud size={31} strokeWidth={1.7} /></div>
          <h2>Upload a receipt or invoice</h2>
          <p>Drag a file here or choose one from your device.</p>
          <div className="actions">
            <button className="primary" disabled={busy} onClick={() => picker.current?.click()}>
              {busy ? <LoaderCircle size={17} className="spin" /> : <UploadCloud size={17} />}
              {busy ? 'Uploading…' : 'Choose file'}
            </button>
            <button disabled={busy} onClick={() => camera.current?.click()}><Camera size={17} /> Take photo</button>
          </div>
          <small>JPEG, PNG, WebP, or PDF · Up to 20 MB</small>
          <input ref={picker} type="file" accept={supported} hidden onChange={event => { void upload(event.target.files?.[0]); event.target.value = '' }} />
          <input ref={camera} type="file" accept="image/*" capture="environment" hidden onChange={event => { void upload(event.target.files?.[0]); event.target.value = '' }} />
        </section>
        {error && <p className="message error" role="alert"><AlertCircle size={18} />{error}</p>}
        {notice && <p className="message" role="status"><CheckCircle2 size={18} />{notice}</p>}
        <section className="library" aria-labelledby="library-heading">
          <div className="library-heading">
            <h2 id="library-heading">Recent uploads</h2>
            <span>{receipts.length} {receipts.length === 1 ? 'document' : 'documents'}</span>
          </div>
          {listError && <div className="empty" role="alert"><p>{listError}</p><button onClick={() => void refresh()}><RotateCw size={16} /> Refresh</button></div>}
          {!listError && loading && <div className="empty" role="status">Loading documents…</div>}
          {!loading && !listError && receipts.length === 0 &&
            <div className="empty"><FileText size={28} strokeWidth={1.6} /><h3>No documents yet</h3><p>Your uploaded receipts and invoices will appear here.</p></div>}
          <ul className="document-list">
            {receipts.map(receipt =>
              <li key={receipt.id} className="document-row">
                {receipt.preview_url ?
                  <button className="thumbnail" aria-label={`Preview ${receipt.filename}`} onClick={() => setSelected(receipt)}>
                    <img src={receipt.preview_url} alt="" />
                  </button> : <span className="thumbnail placeholder"><FileText size={22} /></span>}
                <div className="document-info">
                  <p className="filename" title={receipt.filename}>{receipt.filename}</p>
                  <p className="metadata">{dateFormat.format(new Date(receipt.created_at))}
                    {receipt.page_count !== null && receipt.content_type === 'application/pdf' && ` · ${receipt.page_count} ${receipt.page_count === 1 ? 'page' : 'pages'}`}
                  </p>
                  {receipt.error && <p className="failure">{receipt.error}</p>}
                </div>
                <Status status={receipt.status} />
                {receipt.status === 'failed' && <button className="retry" disabled={retrying !== null} onClick={() => void retry(receipt)}><RotateCw size={14} />{retrying === receipt.id ? 'Retrying…' : 'Retry'}</button>}
              </li>
            )}
          </ul>
        </section>
        <dialog ref={dialog} onClose={() => setSelected(null)} aria-labelledby="preview-title">
          {selected && <>
            <div className="dialog-title"><h2 id="preview-title">{selected.filename}</h2><button aria-label="Close preview" onClick={() => setSelected(null)}><X size={19} /></button></div>
            <img className="preview" src={selected.preview_url ?? undefined} alt={`Preview of ${selected.filename}`} />
            {selected.content_type === 'application/pdf' && <p className="metadata">Page 1 of {selected.page_count}</p>}
            {extraction && <dl className="extracted">
              <div><dt>Merchant</dt><dd>{extraction.fields.merchant ?? 'Unknown'}</dd></div>
              <div><dt>Document date</dt><dd>{extraction.fields.document_date ?? 'Unknown'}</dd></div>
              <div><dt>Printed total</dt><dd>{extraction.fields.total ?? 'Unknown'} {extraction.fields.currency}</dd></div>
              <div><dt>Payment status</dt><dd>{extraction.fields.payment_status}</dd></div>
            </dl>}
            {extraction?.validation && extraction.validation.flags.length > 0 &&
              <section className="validation-flags" aria-labelledby="validation-heading">
                <h3 id="validation-heading">Needs review</h3>
                <ul>
                  {extraction.validation.flags.map(flag =>
                    <li key={`${flag.field}:${flag.code}`}>
                      <strong>{flag.severity === 'blocking' ? 'Required' : 'Check'}</strong>
                      {' · '}{flag.reason}{flag.source && ` (page ${flag.source.page})`}
                    </li>
                  )}
                </ul>
              </section>}
            <a className="original" href={`/api/receipts/${selected.id}/original`}>Download original</a>
          </>}
        </dialog>
      </main>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
