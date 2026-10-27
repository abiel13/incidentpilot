import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity, AlertTriangle, ArrowDownRight, ArrowLeft, ArrowRight, BadgeCheck, BookOpen,
  Check, CheckCircle2, ChevronDown, ChevronRight, CircleHelp, Clock3, Code2, Database,
  Download, ExternalLink, FileText, GitBranch, HardDrive, History, Layers3, LoaderCircle,
  Menu, Plus, Radio, RefreshCw, Search, Server, Settings2, ShieldCheck, Sparkles,
  TerminalSquare, X, XCircle,
} from 'lucide-react'
import { api, type Checkpoint, type Evidence, type Incident, type Investigation, type Lesson, type Run, type TimelineEvent } from './api'

type View = 'dashboard' | 'investigations' | 'memory'
type Tab = 'evidence' | 'checkpoints' | 'memory'

const relativeTime = (value?: string) => {
  if (!value) return 'just now'
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const statusText: Record<string, string> = {
  queued: 'Queued', running: 'Investigating', resuming: 'Resuming', approval_required: 'Awaiting review',
  completed: 'Resolved', rejected: 'Action rejected', inconclusive: 'Inconclusive', recoverable: 'Ready to resume', failed: 'Failed',
}

function StatusBadge({ status }: { status: string }) {
  const live = ['queued', 'running', 'resuming'].includes(status)
  return <span className={`status-badge status-${status}`}><i className={live ? 'pulse-dot' : ''} />{statusText[status] ?? status}</span>
}

function Severity({ level }: { level: string }) {
  return <span className={`severity severity-${level.toLowerCase()}`}><i />{level}</span>
}

function BrandMark() {
  return <div className="brand-mark"><Activity size={17} strokeWidth={2.5} /><span className="brand-spark">·</span></div>
}

function App() {
  const [view, setView] = useState<View>('dashboard')
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [runs, setRuns] = useState<Run[]>([])
  const [lessons, setLessons] = useState<Lesson[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [investigation, setInvestigation] = useState<Investigation | null>(null)
  const [events, setEvents] = useState<TimelineEvent[]>([])
  const [tab, setTab] = useState<Tab>('evidence')
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [newIncidentOpen, setNewIncidentOpen] = useState(false)
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [streamEpoch, setStreamEpoch] = useState(0)
  const selectedRef = useRef<string | null>(null)

  const refreshHome = useCallback(async () => {
    try {
      const [incidentData, runData, memoryData] = await Promise.all([api.incidents(), api.runs(), api.memory()])
      setIncidents(incidentData)
      setRuns(runData)
      setLessons(memoryData)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Unable to reach the IncidentPilot API.')
    }
  }, [])

  useEffect(() => { void refreshHome() }, [refreshHome])

  const refreshRun = useCallback(async (id: string) => {
    try {
      const detail = await api.investigation(id)
      if (selectedRef.current === id) setInvestigation(detail)
      await refreshHome()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not refresh investigation.')
    }
  }, [refreshHome])

  useEffect(() => {
    selectedRef.current = selectedRunId
    if (!selectedRunId) {
      setInvestigation(null)
      setEvents([])
      return
    }
    setEvents([])
    setCheckpoints([])
    void refreshRun(selectedRunId)
    const source = new EventSource(`${api.reportUrl(selectedRunId).replace(/\/report\.md$/, '')}/events`)
    source.addEventListener('update', (raw) => {
      const next = JSON.parse((raw as MessageEvent).data) as TimelineEvent
      setEvents(current => current.some(item => item.id === next.id) ? current : [...current, next])
      if (['approval_required', 'report_completed', 'investigation_failed'].includes(next.kind)) {
        void refreshRun(selectedRunId)
      }
    })
    source.addEventListener('done', () => {
      source.close()
      void refreshRun(selectedRunId)
    })
    source.onerror = () => { /* EventSource retries transient disconnects automatically. */ }
    return () => source.close()
  }, [selectedRunId, streamEpoch, refreshRun])

  useEffect(() => {
    if (!selectedRunId || !investigation || !['queued', 'running', 'resuming'].includes(investigation.status)) return
    const timer = window.setInterval(() => void refreshRun(selectedRunId), 1400)
    return () => window.clearInterval(timer)
  }, [selectedRunId, investigation?.status, refreshRun])

  const chooseRun = async (id: string) => {
    setView('investigations')
    setSelectedRunId(id)
    setMobileNavOpen(false)
    setError('')
    try {
      const detail = await api.investigation(id)
      setInvestigation(detail)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Unable to open investigation.')
    }
  }

  const beginInvestigation = async (incidentId: string) => {
    setBusy(true)
    setError('')
    try {
      const started = await api.start(incidentId)
      await refreshHome()
      setView('investigations')
      setSelectedRunId(started.run_id)
      setStreamEpoch(value => value + 1)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Unable to start investigation.')
    } finally {
      setBusy(false)
    }
  }

  const submitNewIncident = async (value: Pick<Incident, 'title' | 'description' | 'service' | 'severity'>) => {
    setBusy(true)
    setError('')
    try {
      const created = await api.createIncident(value)
      await refreshHome()
      setNewIncidentOpen(false)
      await beginInvestigation(created.id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not create incident.')
      setBusy(false)
    }
  }

  const respond = async (decision: 'approve' | 'reject' | 'request_changes', note?: string) => {
    if (!selectedRunId) return
    setBusy(true)
    setError('')
    try {
      await api.review(selectedRunId, decision, note)
      await refreshRun(selectedRunId)
      setStreamEpoch(value => value + 1)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not submit review.')
    } finally {
      setBusy(false)
    }
  }

  const resumeRun = async () => {
    if (!selectedRunId) return
    setBusy(true)
    setError('')
    try {
      await api.resume(selectedRunId)
      await refreshRun(selectedRunId)
      setStreamEpoch(value => value + 1)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not resume investigation.')
    } finally {
      setBusy(false)
    }
  }

  const loadCheckpoints = async () => {
    if (!selectedRunId) return
    setTab('checkpoints')
    try {
      const result = await api.checkpoints(selectedRunId)
      setCheckpoints(result.checkpoints)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load checkpoint history.')
    }
  }

  const branch = async (checkpointId: string) => {
    if (!selectedRunId) return
    setBusy(true)
    setError('')
    try {
      const next = await api.branch(selectedRunId, checkpointId)
      await refreshHome()
      await chooseRun(next.run_id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not start branch.')
    } finally {
      setBusy(false)
    }
  }

  const nav = (next: View) => {
    setView(next)
    setSelectedRunId(null)
    setMobileNavOpen(false)
    setError('')
  }

  const latestRuns = useMemo(() => runs.slice(0, 8), [runs])
  const currentIncident = incidents.find(item => item.id === investigation?.incident_id)

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNavOpen ? 'sidebar-open' : ''}`}>
        <div className="brand-row"><BrandMark /><span>incident<span className="brand-light">pilot</span></span><span className="brand-version">POC</span></div>
        <div className="workspace-switch"><div className="workspace-avatar">NP</div><div className="workspace-copy"><strong>Northstar Platform</strong><span>Engineering workspace</span></div><ChevronDown size={15} /></div>
        <div className="side-label">WORKSPACE</div>
        <nav className="side-nav">
          <button className={view === 'dashboard' ? 'active' : ''} onClick={() => nav('dashboard')}><Layers3 size={16} />Overview</button>
          <button className={view === 'investigations' ? 'active' : ''} onClick={() => nav('investigations')}><Activity size={16} />Investigations<span className="nav-count">{runs.length}</span></button>
          <button className={view === 'memory' ? 'active' : ''} onClick={() => nav('memory')}><BookOpen size={16} />Verified lessons<span className="nav-count">{lessons.length}</span></button>
        </nav>
        <div className="side-section-heading"><span className="side-label">RECENT RUNS</span><button aria-label="Refresh run list" onClick={() => void refreshHome()}><RefreshCw size={13} /></button></div>
        <div className="run-nav-list">
          {latestRuns.length ? latestRuns.map(run => (
            <button key={run.run_id} className={`run-nav-item ${selectedRunId === run.run_id ? 'selected' : ''}`} onClick={() => void chooseRun(run.run_id)}>
              <span className={`run-state-dot dot-${run.status}`} />
              <span className="run-nav-copy"><strong>{run.incident_title}</strong><small>{relativeTime(run.created_at)} · {statusText[run.status] ?? run.status}</small></span>
            </button>
          )) : <div className="empty-sidebar">Your investigation history will appear here.</div>}
        </div>
        <div className="sidebar-foot">
          <div className="workspace-health"><span className="health-dot" /><span>Simulator online</span><span className="health-label">SIMULATED</span></div>
          <div className="profile-row"><div className="profile-avatar">JD</div><div><strong>Jordan Davis</strong><small>Incident commander</small></div><Settings2 size={16} className="profile-settings" /></div>
        </div>
      </aside>

      {mobileNavOpen && <button className="mobile-scrim" aria-label="Close navigation" onClick={() => setMobileNavOpen(false)} />}
      <main className="main-area">
        <header className="topbar">
          <button className="mobile-menu" aria-label="Open navigation" onClick={() => setMobileNavOpen(true)}><Menu size={20} /></button>
          <div className="breadcrumbs"><span>Northstar Platform</span><ChevronRight size={14} /><strong>{selectedRunId ? 'Investigation' : view === 'memory' ? 'Verified lessons' : view === 'investigations' ? 'Investigations' : 'Overview'}</strong></div>
          <div className="topbar-right"><div className="trace-indicator"><span />DEMO ENVIRONMENT</div><button className="topbar-icon" title="Help"><CircleHelp size={17} /></button><div className="topbar-avatar">JD</div></div>
        </header>
        <div className="page-content">
          {error && <div className="global-error"><AlertTriangle size={16} /><span>{error}</span><button onClick={() => setError('')} aria-label="Dismiss error"><X size={15} /></button></div>}
          {selectedRunId && investigation ? (
            <InvestigationDetail
              key={selectedRunId}
              investigation={investigation}
              events={events}
              incident={currentIncident}
              tab={tab}
              setTab={setTab}
              checkpoints={checkpoints}
              loadCheckpoints={loadCheckpoints}
              branch={branch}
              busy={busy}
              respond={respond}
              resumeRun={resumeRun}
            />
          ) : view === 'memory' ? <MemoryPage lessons={lessons} onOpenRun={chooseRun} /> : view === 'investigations' ? (
            <InvestigationList runs={runs} onOpen={chooseRun} onDashboard={() => nav('dashboard')} />
          ) : (
            <Dashboard incidents={incidents} runs={runs} lessons={lessons} busy={busy} onStart={beginInvestigation} onNew={() => setNewIncidentOpen(true)} onOpen={chooseRun} />
          )}
        </div>
      </main>
      {newIncidentOpen && <NewIncidentModal busy={busy} onClose={() => setNewIncidentOpen(false)} onSubmit={submitNewIncident} />}
    </div>
  )
}

function Dashboard({ incidents, runs, lessons, busy, onStart, onNew, onOpen }: {
  incidents: Incident[]; runs: Run[]; lessons: Lesson[]; busy: boolean;
  onStart: (incidentId: string) => void; onNew: () => void; onOpen: (id: string) => void
}) {
  const activeCount = runs.filter(item => ['queued', 'running', 'resuming', 'approval_required'].includes(item.status)).length
  return (
    <div className="dashboard-page">
      <div className="welcome-row">
        <div><div className="eyebrow"><span className="eyebrow-mark">01</span>INCIDENT RESPONSE WORKSPACE</div><h1>Good morning, Jordan<span className="wave">✳</span></h1><p className="page-subtitle">Bring the right evidence together. Decide what happens next.</p></div>
        <button className="button button-primary" onClick={onNew}><Plus size={16} />Report an incident</button>
      </div>
      <div className="stat-row">
        <div className="stat-card"><span className="stat-icon stat-blue"><Activity size={17} /></span><div className="stat-text"><small>INVESTIGATIONS</small><strong>{runs.length.toString().padStart(2, '0')}</strong></div><span className="stat-foot">all time</span></div>
        <div className="stat-card"><span className="stat-icon stat-orange"><Radio size={17} /></span><div className="stat-text"><small>NEEDS ATTENTION</small><strong>{activeCount.toString().padStart(2, '0')}</strong></div><span className="stat-foot">active or awaiting review</span></div>
        <div className="stat-card"><span className="stat-icon stat-green"><BadgeCheck size={17} /></span><div className="stat-text"><small>VERIFIED LESSONS</small><strong>{lessons.length.toString().padStart(2, '0')}</strong></div><span className="stat-foot">cross-run memory</span></div>
      </div>
      <div className="section-heading"><div><div className="eyebrow muted-eyebrow">START WITH A SCENARIO</div><h2>Ready-to-investigate incidents</h2></div><span className="sim-tag"><ShieldCheck size={13} />SIMULATED DATA</span></div>
      <div className="incident-grid">
        {incidents.slice(0, 3).map((incident, index) => {
          const config = [
            { icon: <ArrowDownRight size={18} />, category: 'DEPLOYMENT REGRESSION', color: 'incident-icon-red', count: '04 signals seeded' },
            { icon: <Database size={18} />, category: 'DATABASE PERFORMANCE', color: 'incident-icon-blue', count: '04 signals seeded' },
            { icon: <Settings2 size={18} />, category: 'CONFIGURATION FAILURE', color: 'incident-icon-violet', count: '04 signals seeded' },
          ][index] ?? { icon: <AlertTriangle size={18} />, category: 'USER REPORTED', color: 'incident-icon-blue', count: 'Report only' }
          return (
            <article className="incident-card" key={incident.id}>
              <div className="incident-card-top"><span className={`incident-icon ${config.color}`}>{config.icon}</span><Severity level={incident.severity} /><button className="card-more" title="Seeded incident"><ChevronDown size={16} /></button></div>
              <div className="incident-category">{config.category}</div><h3>{incident.title}</h3><p>{incident.description}</p>
              <div className="incident-card-meta"><span><Server size={13} />{incident.service}</span><span><HardDrive size={13} />{incident.is_seed ? config.count : 'User report'}</span></div>
              <button className="card-action" disabled={busy} onClick={() => onStart(incident.id)}>Investigate scenario <ArrowRight size={15} /></button>
            </article>
          )
        })}
      </div>
      <div className="bottom-dashboard-grid">
        <section className="panel recent-panel">
          <div className="panel-heading"><div><div className="eyebrow muted-eyebrow">LATEST ACTIVITY</div><h2>Recent investigations</h2></div><button className="link-button" onClick={() => onOpen(runs[0]?.run_id)}>View history <ArrowRight size={14} /></button></div>
          {runs.length ? <div className="recent-list">{runs.slice(0, 4).map(run => <button className="recent-row" key={run.run_id} onClick={() => onOpen(run.run_id)}><span className={`recent-run-icon status-icon-${run.status}`}><FileText size={16} /></span><span className="recent-copy"><strong>{run.incident_title}</strong><small>{run.service} · {relativeTime(run.created_at)}</small></span><StatusBadge status={run.status} /><ChevronRight size={15} className="recent-chevron" /></button>)}</div> : <div className="panel-empty"><div className="empty-icon"><History size={20} /></div><strong>No investigations yet</strong><span>Start with one of the seeded scenarios above.</span></div>}
        </section>
        <section className="panel graph-panel"><div className="panel-heading"><div><div className="eyebrow muted-eyebrow">YOUR WORKFLOW</div><h2>From signal to resolution</h2></div><span className="graph-live"><span />LIVE</span></div><MiniGraph /></section>
      </div>
      <div className="dashboard-note"><div className="note-icon"><ShieldCheck size={16} /></div><div><strong>This workspace is a simulation</strong><span>Incident data and actions are simulated. In Gemini mode, incident context and evidence are sent to Google for diagnosis.</span></div><button onClick={onNew}>Use your own incident <ArrowRight size={14} /></button></div>
    </div>
  )
}

function MiniGraph() {
  const steps = [{ icon: <Search size={14} />, text: 'Gather evidence' }, { icon: <Layers3 size={14} />, text: 'Specialist review' }, { icon: <ShieldCheck size={14} />, text: 'Human approval' }, { icon: <CheckCircle2 size={14} />, text: 'Verify outcome' }]
  return <div className="mini-graph">{steps.map((step, index) => <div className="mini-graph-step" key={step.text}><div className={`mini-graph-node ${index === 2 ? 'approval-node' : ''}`}>{step.icon}</div><span>{step.text}</span>{index < steps.length - 1 && <div className="mini-graph-link"><i /></div>}</div>)}</div>
}

function InvestigationList({ runs, onOpen, onDashboard }: { runs: Run[]; onOpen: (id: string) => void; onDashboard: () => void }) {
  return <div className="list-page"><div className="page-title-row"><div><div className="eyebrow muted-eyebrow">WORKSPACE / HISTORY</div><h1>Investigations</h1><p className="page-subtitle">Every run, its current state, and the review trail.</p></div><button className="button button-subtle" onClick={onDashboard}><ArrowLeft size={15} />Back to overview</button></div>
    <div className="panel investigation-table"><div className="table-head"><span>INCIDENT</span><span>SERVICE</span><span>STATUS</span><span>STARTED</span><span /></div>
      {runs.length ? runs.map(run => <button className="table-row" key={run.run_id} onClick={() => onOpen(run.run_id)}><span className="table-incident"><span className={`table-mark table-mark-${run.status}`}><Activity size={15} /></span><span><strong>{run.incident_title}</strong><small>{run.run_id}{run.source_run_id ? ` · branch of ${run.source_run_id}` : ''}</small></span></span><span className="table-service">{run.service}</span><span><StatusBadge status={run.status} /></span><span className="table-time">{relativeTime(run.created_at)}</span><ChevronRight size={15} /></button>) : <div className="panel-empty"><div className="empty-icon"><History size={20} /></div><strong>No investigation history</strong><span>Start a seeded or custom incident to create a run.</span></div>}
    </div></div>
}

function InvestigationDetail({ investigation, events, incident, tab, setTab, checkpoints, loadCheckpoints, branch, busy, respond, resumeRun }: {
  investigation: Investigation; events: TimelineEvent[]; incident?: Incident; tab: Tab; setTab: (tab: Tab) => void;
  checkpoints: Checkpoint[]; loadCheckpoints: () => void; branch: (checkpointId: string) => void; busy: boolean;
  respond: (decision: 'approve' | 'reject' | 'request_changes', note?: string) => void; resumeRun: () => void
}) {
  const current = investigation.current
  const report = investigation.report
  const diagnosis = current?.diagnosis ?? report?.diagnosis
  const evidence = current?.evidence ?? report?.evidence ?? []
  const findings = current?.findings ?? []
  const remediation = current?.remediation ?? report?.remediation
  const isRunning = ['queued', 'running', 'resuming'].includes(investigation.status)
  const formattedDuration = useMemo(() => investigation.duration_ms ? `${(investigation.duration_ms / 1000).toFixed(1)}s` : '—', [investigation.duration_ms])

  return (
    <div className="investigation-page">
      <div className="detail-back-row"><span><span className="eyebrow muted-eyebrow">INCIDENTS</span><ChevronRight size={13} /> <span>{incident?.title ?? investigation.incident_title}</span></span><a className="text-link" href="https://docs.langchain.com/oss/python/langgraph/graph-api" target="_blank" rel="noreferrer">Graph run <ExternalLink size={12} /></a></div>
      <div className="detail-title-row"><div className="detail-title-main"><div className="detail-icon"><AlertTriangle size={18} /></div><div><div className="title-eyebrow"><Severity level={investigation.severity} /><span>·</span><span>{investigation.service}</span><span>·</span><span className="mono-id">{investigation.run_id}</span></div><h1>{investigation.incident_title}</h1></div></div><div className="detail-actions"><StatusBadge status={investigation.status} /><a className={`button button-subtle button-small ${report ? '' : 'disabled-link'}`} href={report ? api.reportUrl(investigation.run_id) : undefined} aria-disabled={!report}><Download size={14} />Export report</a></div></div>
      <div className="run-meta-strip"><span><Clock3 size={14} />Started {relativeTime(investigation.created_at)}</span><span><TerminalSquare size={14} />{formattedDuration} runtime</span><span><GitBranch size={14} />Thread <code>{investigation.thread_id.slice(-12)}</code></span>{investigation.source_checkpoint_id && <span><GitBranch size={14} />Branch checkpoint <code>{investigation.source_checkpoint_id.slice(-10)}</code></span>}<span className="simulated-strip"><ShieldCheck size={13} />SIMULATED ENVIRONMENT</span></div>
      {investigation.error && <div className="error-card"><AlertTriangle size={17} /><div><strong>Investigation needs attention</strong><span>{investigation.error}</span></div>{investigation.status === 'recoverable' || investigation.status === 'failed' ? <button className="button button-small button-subtle" onClick={resumeRun} disabled={busy}><RefreshCw size={14} />Resume</button> : null}</div>}
      {investigation.status === 'recoverable' && !investigation.error && <div className="recover-card"><div className="recover-symbol"><RefreshCw size={17} /></div><div><strong>Checkpoint recovered</strong><span>This run stopped during a server restart. Its last saved graph state is intact.</span></div><button className="button button-primary button-small" onClick={resumeRun} disabled={busy}>{busy ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}Resume run</button></div>}
      <div className="workflow-rail"><div className={`rail-step done ${events.some(event => event.kind === 'investigation_started') ? '' : 'waiting'}`}><span className="rail-check"><Check size={12} /></span><div><strong>Intake</strong><small>Validated</small></div></div><span className="rail-line done" /><div className={`rail-step ${isRunning && !diagnosis ? 'active' : diagnosis ? 'done' : ''}`}><span className="rail-check">{diagnosis ? <Check size={12} /> : <Search size={12} />}</span><div><strong>Investigate</strong><small>{diagnosis ? `${diagnosis.iterations ?? 1} pass${diagnosis.iterations === 1 ? '' : 'es'}` : isRunning ? 'In progress' : 'Queued'}</small></div></div><span className={`rail-line ${diagnosis ? 'done' : ''}`} /><div className={`rail-step ${investigation.status === 'approval_required' ? 'active' : report || investigation.status === 'rejected' ? 'done' : ''}`}><span className="rail-check">{report || investigation.status === 'rejected' ? <Check size={12} /> : <ShieldCheck size={12} />}</span><div><strong>Review</strong><small>{investigation.status === 'approval_required' ? 'Your decision' : report ? 'Recorded' : 'Pending'}</small></div></div><span className={`rail-line ${report?.status === 'completed' ? 'done' : ''}`} /><div className={`rail-step ${report?.status === 'completed' ? 'done' : ''}`}><span className="rail-check">{report?.status === 'completed' ? <Check size={12} /> : <CheckCircle2 size={13} />}</span><div><strong>Resolve</strong><small>{report?.status === 'completed' ? 'Verified' : investigation.status === 'rejected' ? 'Stopped' : 'Pending'}</small></div></div></div>
      <div className="detail-grid">
        <div className="detail-left-column">
          <section className="panel diagnosis-panel"><div className="panel-topline"><div className="eyebrow muted-eyebrow">WORKING DIAGNOSIS</div>{diagnosis && <span className={`confidence-pill ${diagnosis.confidence >= .8 ? 'confidence-high' : 'confidence-low'}`}><span />{Math.round(diagnosis.confidence * 100)}% {diagnosis.model_call ? 'model-reported confidence' : 'confidence'}</span>}</div>
            {diagnosis ? <><h2>{diagnosis.diagnosis}</h2><div className="diagnosis-foot"><span className="source-tag"><Sparkles size={12} />{diagnosis.source ?? 'Evidence synthesis'}</span><span>{diagnosis.iterations ?? 1} investigation pass{diagnosis.iterations === 1 ? '' : 'es'}</span></div>{diagnosis.model_call && <div className="model-provenance"><strong>{diagnosis.model_call.provider} · {diagnosis.model_call.model}</strong><span>{diagnosis.model_call.stage} · {diagnosis.model_call.status} · {(diagnosis.model_call.duration_ms / 1000).toFixed(1)}s{diagnosis.model_call.usage?.totalTokenCount != null ? ` · ${diagnosis.model_call.usage.totalTokenCount} tokens` : ''}</span>{diagnosis.model_call.response_id && <span>Response ID: {diagnosis.model_call.response_id}</span>}{diagnosis.evidence_ids?.length ? <span>Cited evidence: {diagnosis.evidence_ids.join(', ')}</span> : null}</div>}<div className="uncertainty-note"><CircleHelp size={14} /><span><strong>Remaining uncertainty</strong>{diagnosis.uncertainty}</span></div></> : <div className="diagnosis-loading"><div className="loading-orb"><Sparkles size={18} /></div><div><strong>{investigation.status === 'queued' ? 'Preparing your investigation' : 'Specialists are reviewing evidence'}</strong><span>Concise findings and evidence are streaming into this workspace.</span></div><div className="typing-dots"><i /><i /><i /></div></div>}
          </section>
          <section className="panel specialists-panel"><div className="panel-heading compact-heading"><div><div className="eyebrow muted-eyebrow">SPECIALIST TEAM</div><h2>Parallel investigation</h2></div><span className="parallel-label"><span className="parallel-icon"><Layers3 size={13} /></span>{findings.length ? `${new Set(findings.map(item => item.iteration)).size} round${new Set(findings.map(item => item.iteration)).size === 1 ? '' : 's'}` : '3 specialists'}</span></div>
            <div className="specialist-list">{[
              { id: 'logs', name: 'Application logs', sub: 'Errors & request traces', icon: <TerminalSquare size={15} /> },
              { id: 'changes', name: 'Changes & configuration', sub: 'Deployments, config & runbooks', icon: <GitBranch size={15} /> },
              { id: 'performance', name: 'Performance signals', sub: 'Metrics & resource use', icon: <Activity size={15} /> },
            ].map(specialist => {
              const own = findings.filter(item => item.specialist === specialist.id)
              const liveEvent = [...events].reverse().find(event => event.payload?.specialist === specialist.id && ['specialist_started', 'specialist_completed'].includes(event.kind))
              const active = liveEvent?.kind === 'specialist_started' || (isRunning && !own.length)
              return <div className={`specialist-row ${active ? 'specialist-active' : ''}`} key={specialist.id}><span className="specialist-icon">{specialist.icon}</span><div className="specialist-text"><strong>{specialist.name}</strong><small>{own.length ? `${own.length} finding${own.length === 1 ? '' : 's'} · ${own.map(item => Math.round(item.confidence * 100)).join('% / ')}% confidence` : specialist.sub}</small>{own.length > 0 && <p>{own.map(item => item.summary).join(' ')}</p>}</div><span className={`specialist-state ${own.length ? 'specialist-finished' : active ? 'specialist-working' : ''}`}>{own.length ? <CheckCircle2 size={15} /> : active ? <LoaderCircle className="spin" size={15} /> : <Clock3 size={14} />}{own.length ? 'Done' : active ? 'Working' : 'Waiting'}</span></div>
            })}</div>
          </section>
          <section className="panel investigation-data-panel"><div className="data-tabs"><button className={tab === 'evidence' ? 'selected' : ''} onClick={() => setTab('evidence')}><Search size={14} />Evidence & tools<span>{evidence.length}</span></button><button className={tab === 'checkpoints' ? 'selected' : ''} onClick={() => { if (tab !== 'checkpoints') loadCheckpoints() }}><History size={14} />Graph history</button><button className={tab === 'memory' ? 'selected' : ''} onClick={() => setTab('memory')}><BookOpen size={14} />Memory<span>{current?.memory_lessons?.length ?? 0}</span></button></div>
            {tab === 'evidence' ? <EvidencePanel evidence={evidence} events={events} /> : tab === 'checkpoints' ? <CheckpointPanel checkpoints={checkpoints} onBranch={branch} busy={busy} /> : <LessonPanel lessons={current?.memory_lessons ?? []} />}
          </section>
        </div>
        <div className="detail-right-column">
          {investigation.status === 'approval_required' && remediation ? <ApprovalPanel remediation={remediation} busy={busy} onApprove={() => respond('approve')} onReject={(note) => respond('reject', note)} onRequestChanges={(note) => respond('request_changes', note)} /> : report ? <ReportCard report={report} /> : <ProposalPreview remediation={remediation} />}
          <TimelinePanel events={events} isRunning={isRunning} />
          <div className="simulator-callout"><span className="simulator-icon"><ShieldCheck size={15} /></span><div><strong>Simulation boundary</strong><p>Remediation changes only the simulated environment. In Gemini mode, incident context and simulated evidence are sent to Google.</p></div></div>
        </div>
      </div>
    </div>
  )
}

function EvidencePanel({ evidence, events }: { evidence: Evidence[]; events: TimelineEvent[] }) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const toolEvents = events.filter(item => item.kind.startsWith('tool_'))
  const latestEvents = [...toolEvents].reverse().slice(0, 6)
  return <div className="tab-content evidence-content"><div className="evidence-summary"><span>{evidence.length} cited item{evidence.length === 1 ? '' : 's'}</span><span>Seeded workspace data</span></div>
    {evidence.length ? <div className="evidence-list">{evidence.map((item, index) => <article className="evidence-item" key={item.id}><div className="evidence-marker"><span>{String(index + 1).padStart(2, '0')}</span><i /></div><div className="evidence-body"><div className="evidence-meta"><span className="evidence-source">{item.source.replaceAll('_', ' ')}</span>{item.signal === 'root_cause' && <span className="signal-tag">ROOT CAUSE SIGNAL</span>}<time>{item.timestamp || 'Simulated'}</time></div><strong>{item.summary}</strong>{expanded === item.id ? <><p>{item.detail}</p><code className="evidence-id">{item.id}</code></> : <button className="evidence-expand" onClick={() => setExpanded(item.id)}>Inspect evidence <ChevronDown size={12} /></button>}</div></article>)}</div> : <div className="evidence-empty"><div className="empty-icon"><Search size={18} /></div><strong>Waiting for evidence</strong><span>Specialist queries will show their supporting records here.</span></div>}
    <div className="tool-activity"><div className="tool-activity-title"><span><Code2 size={13} />TOOL ACTIVITY</span><span className="tool-live">{toolEvents.length ? 'OBSERVED' : 'WAITING'}</span></div>{latestEvents.length ? latestEvents.map(event => <div className="tool-event" key={event.id}><span className={`tool-event-icon ${event.kind.includes('failure') ? 'tool-fail' : event.kind.includes('succeeded') ? 'tool-ok' : ''}`}><TerminalSquare size={12} /></span><span><strong>{String(event.payload?.tool ?? 'tool')}</strong><small>{event.message}</small></span>{Boolean(event.payload?.retried) && <span className="retry-tag"><RefreshCw size={11} />retry recovered</span>}</div>) : <div className="tool-idle"><span className="idle-square" />Tool calls and outcomes appear as they happen.</div>}</div>
  </div>
}

function CheckpointPanel({ checkpoints, onBranch, busy }: { checkpoints: Checkpoint[]; onBranch: (id: string) => void; busy: boolean }) {
  return <div className="tab-content checkpoints-content"><div className="checkpoint-explainer"><div className="checkpoint-symbol"><HardDrive size={15} /></div><span><strong>Durable graph snapshots</strong><small>Every node transition is stored in SQLite with this investigation's thread ID.</small></span></div>
    {checkpoints.length ? <div className="checkpoint-list">{checkpoints.map((checkpoint, index) => <div className="checkpoint-row" key={checkpoint.checkpoint_id}><span className={`checkpoint-dot ${checkpoint.interrupted ? 'checkpoint-paused' : ''}`} /><div className="checkpoint-copy"><strong>{checkpoint.node ? String(checkpoint.node).replaceAll('_', ' ') : checkpoint.next[0] ? `Next: ${checkpoint.next[0]}` : index === 0 ? 'Latest snapshot' : 'Saved snapshot'}</strong><small>{checkpoint.created_at ? new Date(checkpoint.created_at).toLocaleString() : 'Timestamp unavailable'} · {checkpoint.evidence_count} evidence · {checkpoint.finding_count} findings</small><code>{checkpoint.checkpoint_id}</code></div><button className="button button-tiny" disabled={busy} onClick={() => onBranch(checkpoint.checkpoint_id)}><GitBranch size={12} />Branch</button></div>)}</div> : <div className="evidence-empty"><LoaderCircle size={18} className="spin" /><strong>Loading checkpoint history</strong><span>Reading snapshots from the graph checkpointer.</span></div>}
  </div>
}

function LessonPanel({ lessons }: { lessons: Lesson[] }) {
  return <div className="tab-content lessons-content">{lessons.length ? lessons.map(lesson => <article className="lesson-card" key={lesson.lesson_id}><span className="lesson-mark"><BookOpen size={15} /></span><div><div className="lesson-meta"><span>VERIFIED LESSON</span><span>{relativeTime(lesson.verified_at)}</span></div><strong>{lesson.title}</strong><p>{lesson.detail}</p><small>Source investigation <code>{lesson.source_run_id}</code></small></div></article>) : <div className="evidence-empty"><div className="empty-icon"><BookOpen size={18} /></div><strong>No recalled lesson</strong><span>Verified lessons from prior completed investigations will appear here when relevant.</span></div>}</div>
}

function ApprovalPanel({ remediation, busy, onApprove, onReject, onRequestChanges }: { remediation: NonNullable<Investigation['current']>['remediation']; busy: boolean; onApprove: () => void; onReject: (note?: string) => void; onRequestChanges: (note: string) => void }) {
  const [mode, setMode] = useState<'view' | 'changes' | 'reject'>('view')
  const [note, setNote] = useState('')
  if (!remediation) return <div className="panel proposal-panel"><div className="proposal-empty"><LoaderCircle size={18} className="spin" />Preparing remediation proposal…</div></div>
  return <section className="panel approval-panel"><div className="approval-head"><div className="approval-icon"><ShieldCheck size={16} /></div><div><div className="eyebrow">HUMAN REVIEW REQUIRED</div><h2>Review the proposed fix</h2></div><span className="approval-lock"><span />PAUSED</span></div>
    <div className="approval-banner"><AlertTriangle size={14} /><span>Nothing will change in the simulated environment until you approve.</span></div>
    <div className="proposal-copy"><div className="proposal-label"><Sparkles size={12} />PROPOSED REMEDIATION {remediation.revision > 0 && <span className="revision-label">REVISION {remediation.revision}</span>}</div><h3>{remediation.summary}</h3><p>{remediation.rationale}</p><div className="change-list">{remediation.changes.length ? remediation.changes.map(change => <div className="change-item" key={change.key}><code>{change.key}</code><ArrowRight size={13} /><strong>{String(change.value)}</strong></div>) : <div className="change-item no-change">No environment change proposed.</div>}</div><div className="verify-preview"><CheckCircle2 size={14} /><span><strong>Verification plan</strong>{remediation.verification}</span></div>{remediation.reviewer_change_request && <div className="reviewer-note"><span>REQUESTED CHANGE</span><p>{remediation.reviewer_change_request}</p></div>}</div>
    {mode === 'view' ? <><div className="approval-actions"><button className="button button-approve" disabled={busy || !remediation.changes.length} onClick={onApprove}>{busy ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}Approve simulated action</button><button className="button button-request" disabled={busy} onClick={() => setMode('changes')}>Request changes</button><button className="button button-reject" disabled={busy} onClick={() => setMode('reject')}>Reject</button></div><div className="approval-foot"><span><Radio size={12} />Awaiting your decision</span><span>Approval is recorded in graph state</span></div></> : <div className="review-note-form"><label>{mode === 'changes' ? 'What should be changed?' : 'Why are you rejecting this action?'}<textarea value={note} onChange={event => setNote(event.target.value)} placeholder={mode === 'changes' ? 'For example: lower analytics concurrency to 10…' : 'Optional note for the incident report…'} rows={3} autoFocus /></label><div><button className="button button-text" disabled={busy} onClick={() => { setMode('view'); setNote('') }}>Cancel</button>{mode === 'changes' ? <button className="button button-primary button-small" disabled={busy || !note.trim()} onClick={() => onRequestChanges(note.trim())}>{busy ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}Submit changes</button> : <button className="button button-danger button-small" disabled={busy} onClick={() => onReject(note.trim())}>{busy ? <LoaderCircle className="spin" size={14} /> : <X size={14} />}Confirm rejection</button>}</div></div>}
  </section>
}

function ProposalPreview({ remediation }: { remediation?: NonNullable<Investigation['current']>['remediation'] }) {
  return <section className="panel proposal-panel"><div className="panel-heading compact-heading"><div><div className="eyebrow muted-eyebrow">REMEDIATION</div><h2>Proposed action</h2></div><span className="pending-tag">{remediation ? 'AWAITING REVIEW' : 'PENDING'}</span></div>{remediation ? <><div className="proposal-preview-copy"><div className="proposal-preview-icon"><ShieldCheck size={15} /></div><div><strong>{remediation.summary}</strong><p>{remediation.rationale}</p></div></div><div className="approval-placeholder"><ShieldCheck size={14} /><span>Execution requires your approval</span><LockText /></div></> : <div className="proposal-empty"><span className="placeholder-lines"><i /><i /><i /></span><span>Proposed remediation appears after specialist review.</span></div>}</section>
}

function LockText() { return <span className="lock-text">LOCKED</span> }

function ReportCard({ report }: { report: NonNullable<Investigation['report']> }) {
  const approved = report.approval?.some(item => item.decision === 'approve')
  return <section className={`panel report-panel ${report.status === 'completed' ? 'report-success' : ''}`}>
    <div className="report-head"><span className="report-icon">{report.status === 'completed' ? <CheckCircle2 size={16} /> : report.status === 'rejected' ? <XCircle size={16} /> : <CircleHelp size={16} />}</span><div><div className="eyebrow muted-eyebrow">FINAL INCIDENT REPORT</div><h2>{report.status === 'completed' ? 'Simulated configuration verified' : report.status === 'rejected' ? 'Action was rejected' : 'Investigation complete'}</h2></div></div>
    <div className="report-diagnosis"><span>DIAGNOSIS</span><p>{report.diagnosis.diagnosis}</p><small>{Math.round(report.diagnosis.confidence * 100)}% {report.diagnosis.model_call ? 'model-reported confidence' : 'confidence'} · {report.evidence.length} evidence item{report.evidence.length === 1 ? '' : 's'}</small></div>
    <div className="report-action-result"><div className="result-row"><span>Human decision</span><strong className={approved ? 'text-green' : 'text-neutral'}>{approved ? 'Approved' : report.approval?.at(-1)?.decision === 'reject' ? 'Rejected' : 'No approval'}</strong></div><div className="result-row"><span>Action executed</span><strong>{report.outcome?.action?.action_id ? 'Yes · simulated' : 'No'}</strong></div><div className="result-row"><span>Configuration check</span><strong className={report.outcome?.verified ? 'text-green' : ''}>{report.outcome?.verified ? 'Passed' : 'Not verified'}</strong></div></div>
    {report.outcome?.verification_summary && <div className="report-uncertainty"><ShieldCheck size={13} /><span>{report.outcome.verification_summary}</span></div>}
    <div className="report-uncertainty"><CircleHelp size={13} /><span>{report.remaining_uncertainty}</span></div><a href={api.reportUrl(report.run_id)} className="report-export"><Download size={14} />Download full report<ArrowRight size={13} /></a>
  </section>
}

function TimelinePanel({ events, isRunning }: { events: TimelineEvent[]; isRunning: boolean }) {
  const visible = [...events].reverse().filter(event => event.kind !== 'node_completed').slice(0, 7).reverse()
  const icons: Record<string, React.ReactNode> = {
    investigation_started: <Radio size={12} />, specialist_started: <Layers3 size={12} />, evidence_found: <Search size={12} />,
    diagnosis_revised: <Sparkles size={12} />, approval_required: <ShieldCheck size={12} />, remediation_executed: <Check size={12} />,
    outcome_verified: <BadgeCheck size={12} />, report_completed: <FileText size={12} />, additional_evidence_requested: <RefreshCw size={12} />,
    tool_retryable_failure: <RefreshCw size={12} />, memory_recalled: <BookOpen size={12} />, lesson_persisted: <BookOpen size={12} />,
  }
  return <section className="panel timeline-panel"><div className="panel-heading compact-heading"><div><div className="eyebrow muted-eyebrow">EXECUTION TRACE</div><h2>Investigation timeline</h2></div><span className={`timeline-live ${isRunning ? 'is-live' : ''}`}><i />{isRunning ? 'LIVE' : 'SAVED'}</span></div>
    <div className="timeline-list">{visible.length ? visible.map(event => <div className={`timeline-entry ${event.kind}`} key={event.id}><div className={`timeline-node ${event.kind === 'tool_retryable_failure' ? 'node-warning' : ''}`}>{icons[event.kind] ?? <Activity size={12} />}</div><div className="timeline-body"><strong>{event.message}</strong><small>{event.payload?.specialist ? `${String(event.payload.specialist).replaceAll('_', ' ')} · ` : ''}{new Date(event.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</small></div></div>) : <div className="timeline-empty"><LoaderCircle className="spin" size={15} />Waiting for graph events…</div>}</div>
    {events.length > 7 && <div className="timeline-foot">Showing 7 latest meaningful events <span>{events.length} total</span></div>}
  </section>
}

function MemoryPage({ lessons, onOpenRun }: { lessons: Lesson[]; onOpenRun: (id: string) => void }) {
  return <div className="memory-page"><div className="page-title-row"><div><div className="eyebrow muted-eyebrow">WORKSPACE / PERSISTENT CONTEXT</div><h1>Verified lessons</h1><p className="page-subtitle">Cross-investigation memory is written only after a simulated action passes verification.</p></div><span className="memory-count"><BookOpen size={15} />{lessons.length} lesson{lessons.length === 1 ? '' : 's'}</span></div><div className="memory-explainer"><div className="memory-explainer-icon"><Layers3 size={18} /></div><div><strong>Two kinds of memory, each with a clear scope</strong><p>Checkpoint history belongs to a single investigation thread. Verified lessons below persist across runs, keep a link to their source incident, and are recalled only for matching incident categories.</p></div></div>{lessons.length ? <div className="memory-grid">{lessons.map(lesson => <article className="panel memory-card" key={lesson.lesson_id}><div className="memory-card-top"><span className="memory-card-icon"><BookOpen size={16} /></span><span className="memory-verified"><CheckCircle2 size={12} />VERIFIED</span></div><div className="memory-kind">{lesson.kind.replaceAll('_', ' ')}</div><h2>{lesson.title}</h2><p>{lesson.detail}</p><div className="memory-card-foot"><span>Source run <code>{lesson.source_run_id}</code></span><button onClick={() => onOpenRun(lesson.source_run_id)}>Open <ArrowRight size={13} /></button></div></article>)}</div> : <div className="panel memory-empty"><div className="empty-icon"><BookOpen size={22} /></div><h2>No lessons saved yet</h2><p>Complete a simulated investigation and verify its action. The resulting lesson will be available to future investigations of the same incident type.</p><span><ShieldCheck size={14} />Lessons are evidence-linked and source-attributed</span></div>}</div>
}

function NewIncidentModal({ busy, onClose, onSubmit }: { busy: boolean; onClose: () => void; onSubmit: (value: Pick<Incident, 'title' | 'description' | 'service' | 'severity'>) => void }) {
  const [title, setTitle] = useState('')
  const [service, setService] = useState('')
  const [severity, setSeverity] = useState('high')
  const [description, setDescription] = useState('')
  const [formError, setFormError] = useState('')
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (title.trim().length < 5 || service.trim().length < 2 || description.trim().length < 20) {
      setFormError('Add a title, service, and at least 20 characters of incident detail.')
      return
    }
    onSubmit({ title: title.trim(), service: service.trim(), severity, description: description.trim() })
  }
  return <div className="modal-scrim" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose() }}><form className="incident-modal" onSubmit={submit}><div className="modal-header"><div className="modal-icon"><AlertTriangle size={18} /></div><div><div className="eyebrow muted-eyebrow">NEW INCIDENT</div><h2>Report a signal</h2></div><button type="button" className="modal-close" onClick={onClose} disabled={busy}><X size={17} /></button></div><div className="modal-simulation-note"><ShieldCheck size={14} /><span>This report will run against user-provided incident context and generic simulated runbooks.</span></div><label>Incident title<input value={title} onChange={event => setTitle(event.target.value)} placeholder="What changed or stopped working?" autoFocus /></label><div className="form-two-col"><label>Service name<input value={service} onChange={event => setService(event.target.value)} placeholder="e.g. checkout-api" /></label><label>Severity<select value={severity} onChange={event => setSeverity(event.target.value)}><option value="critical">Critical</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select></label></div><label>What are you observing?<textarea value={description} onChange={event => setDescription(event.target.value)} rows={5} placeholder="Include the symptoms, time window, recent changes, and impact. The simulator will not invent additional telemetry." /></label>{formError && <div className="form-error"><AlertTriangle size={14} />{formError}</div>}<div className="modal-actions"><button type="button" className="button button-subtle" disabled={busy} onClick={onClose}>Cancel</button><button className="button button-primary" disabled={busy}>{busy ? <LoaderCircle className="spin" size={15} /> : <Activity size={15} />}Create & investigate</button></div></form></div>
}

export default App
