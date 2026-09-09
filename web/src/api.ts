export async function api<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(response.status === 401 ? 'AUTH' : typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail))
  }
  return response.json()
}
export interface Environment { id: string; name: string; description: string }
export interface Evidence { id: string; component_id: string; source: string; action: string; ok: boolean; observed_at: string; duration_ms: number; data: string }
export interface Component { id: string; environment_id: string; name: string; type: string; endpoint: string; description: string; settings: Record<string, unknown>; dependencies: string[]; enabled: boolean; agent_state: string; last_seen?: string; observation?: Evidence }
export interface Adapter { type: string; name: string; settings: Record<string, unknown>; secret_fields: string[] }
export interface Assessment { summary: string; hypotheses: {cause: string; evidence_ids: string[]}[]; missing_data: string[]; next_checks: string[] }
export interface Investigation { id: string; status: string; created_at: string; progress: string; request: {question: string}; assessment?: Assessment; error?: string; results: {component: string; assessment?: Assessment; evidence: Evidence[]; error?: string}[] }
