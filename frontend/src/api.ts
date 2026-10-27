export const API = import.meta.env.VITE_API_BASE_URL ?? (import.meta.env.DEV ? 'http://127.0.0.1:8000/api' : '/api')

export type Incident = {
  id: string
  title: string
  description: string
  service: string
  severity: string
  kind: string
  is_seed: boolean
  investigation_count: number
}

export type Run = {
  run_id: string
  thread_id: string
  incident_id: string
  status: string
  created_at: string
  updated_at: string
  incident_title: string
  service: string
  severity: string
  report?: Report | null
  source_run_id?: string | null
  source_checkpoint_id?: string | null
  error?: string | null
  duration_ms?: number | null
}

export type Evidence = {
  id: string
  source_record_id?: string
  source: string
  timestamp?: string
  summary: string
  detail: string
  signal?: string
  iteration?: number
}

export type Finding = {
  finding_id: string
  specialist: string
  iteration: number
  summary: string
  confidence: number
  evidence_ids: string[]
  tools: string[]
}

export type Lesson = {
  lesson_id: string
  source_run_id: string
  incident_id: string
  kind: string
  title: string
  detail: string
  verified_at?: string
}

export type Diagnosis = {
  diagnosis: string
  confidence: number
  uncertainty: string
  iterations?: number
  source?: string
  evidence_ids?: string[]
  model_call?: {
    provider: string
    model: string
    model_version?: string
    stage: string
    status: string
    duration_ms: number
    response_id?: string
    usage?: { promptTokenCount?: number; candidatesTokenCount?: number; totalTokenCount?: number }
  }
}

export type Remediation = {
  summary: string
  rationale: string
  changes: { key: string; value: unknown }[]
  verification: string
  simulated: boolean
  revision: number
  reviewer_change_request?: string
}

export type Report = {
  run_id: string
  incident_id: string
  incident_title: string
  service: string
  severity: string
  status: string
  diagnosis: Diagnosis
  evidence: Evidence[]
  remediation: Remediation
  approval: { decision: string; note?: string }[]
  outcome: { verified?: boolean; verification_summary?: string; action?: { action_id: string; before: object; after: object } }
  remaining_uncertainty: string
  memory_used: Lesson[]
  generated_at: string
  environment_label: string
}

export type Investigation = Run & {
  current?: {
    classification?: { kind: string; summary: string; route: string[] }
    diagnosis?: Diagnosis
    evidence: Evidence[]
    findings: Finding[]
    remediation?: Remediation
    approvals: { decision: string; note?: string }[]
    outcome?: Report['outcome']
    memory_lessons: Lesson[]
  }
}

export type TimelineEvent = {
  id: number
  run_id: string
  kind: string
  message: string
  payload: Record<string, unknown>
  created_at: string
}

export type Checkpoint = {
  checkpoint_id: string
  created_at?: string
  next: string[]
  node?: string | number
  evidence_count: number
  finding_count: number
  approval_count: number
  has_diagnosis: boolean
  interrupted: boolean
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  })
  if (!response.ok) {
    const problem = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(problem.detail ?? `Request failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

export const api = {
  incidents: () => request<Incident[]>('/incidents'),
  createIncident: (value: Pick<Incident, 'title' | 'description' | 'service' | 'severity'>) => request<Incident>('/incidents', { method: 'POST', body: JSON.stringify(value) }),
  runs: () => request<Run[]>('/investigations'),
  investigation: (id: string) => request<Investigation>(`/investigations/${id}`),
  start: (incidentId: string) => request<{ run_id: string; thread_id: string; incident_id: string; status: string }>('/investigations', { method: 'POST', body: JSON.stringify({ incident_id: incidentId }) }),
  review: (id: string, decision: 'approve' | 'reject' | 'request_changes', change_request?: string) => request(`/investigations/${id}/review`, { method: 'POST', body: JSON.stringify({ decision, change_request }) }),
  resume: (id: string) => request(`/investigations/${id}/resume`, { method: 'POST' }),
  checkpoints: (id: string) => request<{ checkpoints: Checkpoint[] }>(`/investigations/${id}/checkpoints`),
  branch: (id: string, checkpointId: string) => request<{ run_id: string }>(`/investigations/${id}/branch?checkpoint_id=${encodeURIComponent(checkpointId)}`, { method: 'POST' }),
  memory: () => request<Lesson[]>('/memory'),
  reportUrl: (id: string) => `${API}/investigations/${id}/report.md`,
}
