import { useState } from 'react'
import { ReceiptText } from 'lucide-react'

export type WorkspaceIdentity = { id: string; email: string; workspace_id: string; workspace_name: string }

export async function authError(response: Response): Promise<string> {
  const detail = (await response.json().catch(() => null))?.detail
  return typeof detail === 'string' ? detail : 'Could not connect. Try again.'
}

export default function SignIn({ onSignedIn }: { onSignedIn: (account: WorkspaceIdentity) => void }) {
  const [register, setRegister] = useState(false)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [workspace, setWorkspace] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit() {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const response = await fetch(register ? '/api/auth/register' : '/api/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(register ? { email, password, workspace_name: workspace } : { email, password }),
      })
      if (!response.ok) throw new Error(await authError(response))
      onSignedIn(await response.json())
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : 'Could not sign in.')
    } finally {
      setBusy(false)
    }
  }

  return <main className="access-page">
    <section className="access-card" aria-labelledby="access-title">
      <span className="brand-icon"><ReceiptText size={22} /></span>
      <h1 id="access-title">{register ? 'Create your business workspace' : 'Sign in to Expense Scanner'}</h1>
      <form onSubmit={event => {
        event.preventDefault()
        void submit()
      }}>
        {register && <label>Business name
          <input autoComplete="organization" value={workspace} onChange={e => setWorkspace(e.target.value)} maxLength={100} required />
        </label>}
        <label>Email
          <input type="email" autoComplete="username" value={email} onChange={e => setEmail(e.target.value)} maxLength={254} required />
        </label>
        <label>Password
          <input type="password" autoComplete={register ? 'new-password' : 'current-password'} value={password}
            onChange={e => setPassword(e.target.value)} minLength={register ? 15 : undefined} maxLength={128} required />
        </label>
        {register && <p className="metadata">Use 15 or more characters.</p>}
        {error && <p className="message error" role="alert">{error}</p>}
        <button type="submit" className="primary" disabled={busy}>
          {busy ? 'Please wait…' : register ? 'Create workspace' : 'Sign in'}
        </button>
      </form>
      <button type="button" disabled={busy} onClick={() => {
        setRegister(!register)
        setPassword('')
        setError('')
      }}>{register ? 'Back to sign in' : 'Create a workspace'}</button>
    </section>
  </main>
}
