export const sessionExpired = 'scanner-session-expired'

export async function workspaceFetch(path: string, options?: RequestInit): Promise<Response> {
  const response = await fetch(path, options)
  if (response.status === 401) window.dispatchEvent(new Event(sessionExpired))
  return response
}
