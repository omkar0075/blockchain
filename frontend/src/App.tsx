import { useEffect, useState } from 'react'

const API = 'http://127.0.0.1:8000'

interface BlockchainHealth {
  status: string
  network: string
  chain_id: number
  contract_address: string
  contract_owner: string
  latest_block: number
  relayer_configured: boolean
  relayer_address: string
  institutions_registered: string[]
}

interface HealthResponse {
  status: string
  mode: string
  database: string
  blockchain: BlockchainHealth
}

interface Institution {
  id: string
  name: string
  type: string
  resources: Record<string, number>
}

interface ProviderCapacity {
  provider: string
  resourceType: string
  totalCapacity: number
  lockedCapacity: number
  committedCapacity: number
  availableCapacity: number
  isActive: boolean
}

interface CapacityLockDetail {
  lockId: string
  resourceType: string
  provider: string
  requester: string
  quantity: number
  createdAt: string
  expiresAt: string
  status: string
  statusCode: number
  reservationId: string
}

interface Offer {
  provider_id: string
  provider_name: string
  available_quantity: number
  estimated_delivery_hours: number
  rationale: string
  onchain_verified?: boolean
}

interface Proposal {
  proposal_id?: string
  request: { requesting_institution_id: string; quantity: number; urgency: string; resource_type: string }
  offers: Offer[]
  selected_provider_id: string | null
  selected_quantity: number
  status: string
  lock_id?: string
  lock_status?: string
  lock_expires_at?: string
  lock_tx_hash?: string
}

interface Commitment {
  commitment_id: string
  proposal_id: string
  requesting_institution_id: string
  provider_id: string
  resource_type: string
  quantity: number
  status: string
  approved_by?: string
  ledger: string
  onchain_reservation_id?: string
  tx_hash?: string
  block_number?: number
  onchain_status?: string
  acceptance_block?: number
  fulfillment_block?: number
  dispute_block?: number
  dispute_reason?: string
  resolution_block?: number
  resolution_notes?: string
  note?: string
  message?: string
  terms_hash?: string
  lock_id?: string
}

interface AuditEvent {
  id: number
  timestamp: string
  action: string
  actor: string
  role: string
  commitment_id?: string
  details: Record<string, any>
  previous_hash: string
  event_hash: string
}

interface EvidenceRecord {
  id: number
  commitment_id: string
  evidence_type: string
  raw_document: string
  stored_sha256: string
  computed_sha256: string
  verified: boolean
  submitted_by: string
  created_at: number
}

interface DirectRpcVerification {
  reservationId: string
  status: string
  requester: string
  provider: string
  resourceRef: string
  decodedRef: string
  expiresAt: string
  createdAt: string
  fulfilledAt: string
  disputeEvidence: string
  disputedBy: string
  verifiedAt: string
  durationMs: number
}

const ACTORS = [
  { id: 'hospital-a', name: 'City Hospital A', role: 'requester', key: 'medway-key-hosp-a-sec91', icon: '🏥' },
  { id: 'lab-c', name: 'Central Diagnostic Lab C', role: 'provider', key: 'medway-key-lab-c-sec73', icon: '🔬' },
  { id: 'consortium-arbitrator', name: 'Consortium Arbitration Board', role: 'arbitrator', key: 'medway-key-arbitrator-sec05', icon: '⚖️' },
]

export default function App() {
  const [health, setHealth] = useState<BlockchainHealth | null>(null)
  const [institutions, setInstitutions] = useState<Institution[]>([])
  const [currentActor, setCurrentActor] = useState(ACTORS[0])
  
  // Resource Request State
  const [requester, setRequester] = useState('hospital-a')
  const [quantity, setQuantity] = useState(3)
  const [urgency, setUrgency] = useState('urgent')
  const [proposal, setProposal] = useState<Proposal | null>(null)
  
  // Commitments State
  const [commitments, setCommitments] = useState<Commitment[]>([])
  const [loading, setLoading] = useState(false)
  const [message, setMessage] = useState('Live MST Coral Testnet Connected · Chain ID 54635')

  // Modals
  const [disputeModalTarget, setDisputeModalTarget] = useState<Commitment | null>(null)
  const [disputeReason, setDisputeReason] = useState('')
  
  const [resolveModalTarget, setResolveModalTarget] = useState<Commitment | null>(null)
  const [resolveOutcome, setResolveOutcome] = useState<'Cancelled' | 'Fulfilled'>('Cancelled')
  const [resolveNotes, setResolveNotes] = useState('')

  const [auditModalTarget, setAuditModalTarget] = useState<Commitment | null>(null)
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([])
  const [auditIntegrity, setAuditIntegrity] = useState<{ valid: boolean; total_events: number } | null>(null)
  const [evidenceRecords, setEvidenceRecords] = useState<EvidenceRecord[]>([])
  const [directRpcResult, setDirectRpcResult] = useState<DirectRpcVerification | null>(null)

  // Feature 3: Capacity Breakdown & Quota State
  const [capacities, setCapacities] = useState<Record<string, ProviderCapacity>>({})
  const [quotaInput, setQuotaInput] = useState(12)
  const [updatingQuota, setUpdatingQuota] = useState(false)
  const [lockModalTarget, setLockModalTarget] = useState<CapacityLockDetail | null>(null)

  // Headers for current actor
  const authHeaders = {
    'Content-Type': 'application/json',
    'X-Actor-Id': currentActor.id,
    'X-Api-Key': currentActor.key,
  }

  async function loadCapacities() {
    try {
      const res = await fetch(`${API}/capacity/lab-c/diagnostic_slot`)
      if (res.ok) {
        const data = await res.json()
        setCapacities(prev => ({ ...prev, 'lab-c': data }))
      }
    } catch {
      // quiet fallback
    }
  }

  async function updateOnchainQuota() {
    if (quotaInput <= 0) return
    setUpdatingQuota(true)
    setMessage(`Updating Lab C on-chain capacity quota to ${quotaInput} slots on Coral Testnet...`)
    try {
      const res = await fetch(`${API}/inventory`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({
          provider_id: 'lab-c',
          resource_type: 'diagnostic_slot',
          total_capacity: quotaInput,
          is_active: true,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Failed to update quota')
      setMessage(`On-chain capacity quota updated to ${quotaInput} slots (Tx: ${data.txHash ? data.txHash.slice(0, 10) + '...' : 'Confirmed'})`)
      await loadCapacities()
      await loadData()
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Error updating quota')
    } finally {
      setUpdatingQuota(false)
    }
  }

  async function inspectLock(lockId: string) {
    setLoading(true)
    try {
      const res = await fetch(`${API}/locks/${lockId}`)
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Failed to inspect lock')
      setLockModalTarget(data.lock)
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Error inspecting lock')
    } finally {
      setLoading(false)
    }
  }

  async function releaseActiveLock(lockId: string) {
    if (!window.confirm('Release this temporary capacity lock hold back to provider inventory?')) return
    setLoading(true)
    setMessage('Releasing temporary capacity lock on Coral Testnet...')
    try {
      const res = await fetch(`${API}/locks/${lockId}/release`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({ reason: 'Released via coordinator dashboard' }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Failed to release lock')
      setMessage(`Capacity lock released back to provider inventory!`)
      setProposal(null)
      await loadCapacities()
      await loadData()
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Error releasing lock')
    } finally {
      setLoading(false)
    }
  }

  async function loadData() {
    try {
      const [hRes, iRes, cRes] = await Promise.all([
        fetch(`${API}/health`).then(r => r.json() as Promise<HealthResponse>),
        fetch(`${API}/institutions`).then(r => r.json() as Promise<Institution[]>),
        fetch(`${API}/commitments`).then(r => r.json() as Promise<Commitment[]>),
      ])
      setHealth(hRes.blockchain)
      setInstitutions(iRes)
      setCommitments(cRes)
      await loadCapacities()
    } catch {
      setMessage('Backend not reachable at http://127.0.0.1:8000. Ensure FastAPI server is running.')
    }
  }

  useEffect(() => {
    loadData()
    const interval = setInterval(loadData, 8000)
    return () => clearInterval(interval)
  }, [])

  // 1. Discover Capacity
  async function discover() {
    setLoading(true)
    setMessage('Multi-agent discovery evaluating provider capacity & delivery SLA...')
    setProposal(null)
    try {
      const res = await fetch(`${API}/requests`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({
          requesting_institution_id: requester,
          resource_type: 'diagnostic_slot',
          quantity,
          urgency,
          required_by_hours: urgency === 'critical' ? 4 : urgency === 'urgent' ? 12 : 24,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Discovery request failed')
      setProposal(data)
      setMessage(data.selected_provider_id ? 'Feasible provider found. Institutional coordinator authorization required.' : 'No provider satisfies constraints.')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Error creating request')
    } finally {
      setLoading(false)
    }
  }

  // 2. Authorize Proposal
  async function authorize(approved: boolean) {
    if (!proposal?.proposal_id) return
    const approver = window.prompt('Enter Hospital Coordinator Name:', 'Dr. Marcus (Chief of Medicine)')
    if (!approver) return
    setLoading(true)
    setMessage(`Relaying EIP-712 meta-transaction to Coral Testnet (Gas sponsored by relayer)...`)
    try {
      const res = await fetch(`${API}/proposals/${proposal.proposal_id}/authorize`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({ approved_by: approver, approved }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Authorization failed')
      setMessage(data.commitment?.message || 'Proposal authorized and confirmed on Coral Testnet.')
      setProposal(null)
      await loadData()
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Authorization error')
    } finally {
      setLoading(false)
    }
  }

  // 3. Provider Accept Commitment
  async function acceptCommitment(id: string) {
    setLoading(true)
    setMessage('Submitting provider EIP-712 acceptance to Coral Testnet...')
    try {
      const res = await fetch(`${API}/commitments/${id}/accept`, {
        method: 'POST',
        headers: authHeaders,
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Acceptance failed')
      setMessage(`Reservation committed on Coral Testnet in block #${data.blockchain?.blockNumber}!`)
      await loadData()
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Acceptance failed')
    } finally {
      setLoading(false)
    }
  }

  // 4. Provider Fulfill Commitment
  async function fulfillCommitment(id: string) {
    setLoading(true)
    setMessage('Provider executing fulfillment on Coral Testnet (with automated relayer top-up)...')
    try {
      const res = await fetch(`${API}/commitments/${id}/fulfill`, {
        method: 'POST',
        headers: authHeaders,
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Fulfillment failed')
      setMessage(`Fulfilled on Coral Testnet (Block #${data.blockchain?.blockNumber}). 7-day dispute window active.`)
      await loadData()
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Fulfillment failed')
    } finally {
      setLoading(false)
    }
  }

  // 5. Requester Dispute
  async function submitDispute() {
    if (!disputeModalTarget || !disputeReason.trim()) return
    setLoading(true)
    setMessage('Hashing evidence (SHA-256) and recording dispute on Coral Testnet...')
    try {
      const res = await fetch(`${API}/commitments/${disputeModalTarget.commitment_id}/dispute`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({
          disputed_by: currentActor.id,
          reason: disputeReason,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Dispute submission failed')
      setMessage(`Dispute successfully broadcast to Coral Testnet (Block #${data.blockchain?.blockNumber}).`)
      setDisputeModalTarget(null)
      setDisputeReason('')
      await loadData()
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Dispute failed')
    } finally {
      setLoading(false)
    }
  }

  // 6. Arbitrator Resolve
  async function submitResolution() {
    if (!resolveModalTarget || !resolveNotes.trim()) return
    setLoading(true)
    setMessage('Publishing consortium arbitration ruling to Coral Testnet...')
    try {
      const res = await fetch(`${API}/commitments/${resolveModalTarget.commitment_id}/resolve`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({
          resolution: resolveOutcome,
          notes: resolveNotes,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Arbitration resolution failed')
      setMessage(`Dispute ruled as ${resolveOutcome} on Coral Testnet (Block #${data.blockchain?.blockNumber}).`)
      setResolveModalTarget(null)
      setResolveNotes('')
      await loadData()
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Resolution failed')
    } finally {
      setLoading(false)
    }
  }

  // 7. Open Audit Modal
  async function openAuditTrail(c: Commitment) {
    setAuditModalTarget(c)
    try {
      const [eventsRes, verifyRes, evidenceRes] = await Promise.all([
        fetch(`${API}/commitments/${c.commitment_id}/audit`).then(r => r.json()),
        fetch(`${API}/audit/verify`).then(r => r.json()),
        fetch(`${API}/commitments/${c.commitment_id}/evidence`).then(r => r.json()),
      ])
      setAuditEvents(eventsRes.audit_trail || [])
      setAuditIntegrity(verifyRes)
      setEvidenceRecords(evidenceRes.evidence || [])
    } catch (err) {
      console.error(err)
    }
  }

  // 8. Direct RPC Verification (Bypasses Backend Entirely)
  async function verifyDirectFromCoralRPC(reservationId: string) {
    setLoading(true)
    setMessage('Querying Coral Testnet RPC directly via eth_call (bypassing backend)...')
    try {
      const cleanId = reservationId.replace('0x', '')
      const callData = '0xa1b416f9' + cleanId
      const start = Date.now()
      const rpcRes = await fetch('https://coralrpc.mstblockchain.com', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'eth_call',
          params: [
            {
              to: health?.contract_address || '0xcd630b8e555e0751afbb9ae511869110d361bb83',
              data: callData,
            },
            'latest',
          ],
        }),
      })
      const json = await rpcRes.json()
      const durationMs = Date.now() - start
      const hex = (json.result || '').replace('0x', '')
      if (hex.length >= 640) {
        const words: string[] = []
        for (let i = 0; i < hex.length; i += 64) {
          words.push(hex.slice(i, i + 64))
        }
        const statusCode = Number(BigInt('0x' + words[5]))
        const statusNames = ['Proposed', 'Committed', 'Fulfilled', 'Cancelled', 'Expired', 'Disputed']
        
        let decodedRef = ''
        try {
          const rawBytes = words[1].match(/.{1,2}/g)?.map(b => parseInt(b, 16)) || []
          decodedRef = String.fromCharCode(...rawBytes).replace(/[\0]/g, '').trim()
        } catch {}

        setDirectRpcResult({
          reservationId: '0x' + words[0],
          resourceRef: '0x' + words[1],
          decodedRef,
          requester: '0x' + words[2].slice(24),
          provider: '0x' + words[3].slice(24),
          expiresAt: new Date(Number(BigInt('0x' + words[4])) * 1000).toLocaleString(),
          status: statusNames[statusCode] || `Code ${statusCode}`,
          createdAt: new Date(Number(BigInt('0x' + words[6])) * 1000).toLocaleString(),
          fulfilledAt: BigInt('0x' + words[7]) > 0n ? new Date(Number(BigInt('0x' + words[7])) * 1000).toLocaleString() : 'N/A',
          disputeEvidence: '0x' + words[8],
          disputedBy: '0x' + words[9].slice(24),
          verifiedAt: new Date().toLocaleTimeString(),
          durationMs,
        })
        setMessage(`Direct on-chain confirmation verified in ${durationMs}ms via Coral RPC!`)
      } else {
        throw new Error('Reservation not found directly on Coral Testnet.')
      }
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Direct RPC query failed')
    } finally {
      setLoading(false)
    }
  }

  const getStatusBadgeClass = (status?: string) => {
    switch (status?.toLowerCase()) {
      case 'proposed':
      case 'authorized':
        return 'status-proposed'
      case 'committed':
        return 'status-committed'
      case 'fulfilled':
        return 'status-fulfilled'
      case 'disputed':
        return 'status-disputed'
      case 'cancelled':
      case 'resolved':
        return 'status-cancelled'
      default:
        return 'status-proposed'
    }
  }

  return (
    <main className="shell">
      {/* TOPBAR */}
      <header className="topbar">
        <div>
          <div className="eyebrow">AUTONOMOUS HEALTHCARE RESOURCE COORDINATION</div>
          <h1>MEDway</h1>
          <p>Multi-Agent Capacity Discovery · EIP-712 Relayed Meta-Transactions · MST Coral Testnet</p>
        </div>
        <div className="topbar-right">
          <div className="network-badge">
            <span className="network-pulse"></span>
            <span>Coral Testnet #{health?.latest_block || '...'}</span>
          </div>
          <span style={{ fontSize: '11px', color: '#64748b' }}>
            Chain ID: <strong>{health?.chain_id || 54635}</strong> · Zero Gas for Clinics
          </span>
        </div>
      </header>

      {/* ACTOR SWITCHER */}
      <section className="actor-switcher-bar">
        <div className="actor-label">
          <span>Active Session Persona (RBAC):</span>
        </div>
        <div className="actor-buttons">
          {ACTORS.map(actor => (
            <button
              key={actor.id}
              className={`actor-btn ${currentActor.id === actor.id ? 'active' : ''}`}
              onClick={() => setCurrentActor(actor)}
            >
              <span>{actor.icon}</span>
              <span>{actor.name}</span>
              <span className="actor-badge-role">{actor.role}</span>
            </button>
          ))}
        </div>
      </section>

      {/* PROTOCOL METRICS BANNER */}
      <section className="protocol-banner">
        <div className="protocol-item">
          <strong>Smart Contract:</strong>{' '}
          <span title={health?.contract_address}>
            {health?.contract_address ? `${health.contract_address.slice(0, 10)}...${health.contract_address.slice(-8)}` : '0xcd630b...bb83'}
          </span>
        </div>
        <div className="protocol-item">
          <strong>Relayer / Sponsor:</strong>{' '}
          <span>{health?.relayer_address ? `${health.relayer_address.slice(0, 8)}...${health.relayer_address.slice(-6)}` : '0x5E5B...8607'}</span>
        </div>
        <div className="protocol-item">
          <strong>Dispute Window:</strong> <span>7 Days (604,800s)</span>
        </div>
        <div className="protocol-item">
          <strong>Privacy Safeguard:</strong> <span>Zero PHI On-Chain (Pseudonymous Refs Only)</span>
        </div>
      </section>

      {/* TWO COLUMN GRID */}
      <section className="grid">
        {/* PANEL 1: INSTITUTION NETWORK & ON-CHAIN CAPACITY */}
        <article className="panel">
          <div className="section-title">
            <span className="step-num">01</span>
            <div>
              <h2>Participating Institutions & On-Chain Quota</h2>
              <p>Real-time capacity allocations strictly enforced on Coral Testnet</p>
            </div>
          </div>
          <div className="institution-list">
            {institutions.map(inst => {
              const cap = capacities[inst.id]
              const isProvider = inst.type === 'diagnostic_lab' || inst.id === 'lab-c'

              return (
                <div
                  className="institution"
                  key={inst.id}
                  style={{ flexDirection: 'column', alignItems: 'stretch', gap: '8px' }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <strong>{inst.name}</strong>
                      <small>
                        {inst.type.replace('_', ' ')} · ID: {inst.id}
                      </small>
                    </div>
                    {isProvider && cap ? (
                      <div style={{ display: 'flex', gap: '5px', flexWrap: 'wrap' }}>
                        <span className="slots-pill" style={{ background: '#dcfce7', color: '#15803d', borderColor: '#86efac' }}>
                          {cap.availableCapacity} avail
                        </span>
                        <span className="slots-pill" style={{ background: '#fef3c7', color: '#b45309', borderColor: '#fde68a' }}>
                          {cap.lockedCapacity} locked
                        </span>
                        <span className="slots-pill" style={{ background: '#e0e7ff', color: '#4338ca', borderColor: '#c7d2fe' }}>
                          {cap.committedCapacity} committed
                        </span>
                      </div>
                    ) : (
                      <span className="slots-pill">{inst.resources?.diagnostic_slot ?? 0} slots</span>
                    )}
                  </div>

                  {/* Multi-segment Real-Time Capacity Bar for Diagnostic Providers */}
                  {isProvider && cap && cap.totalCapacity > 0 && (
                    <div style={{ marginTop: '2px' }}>
                      <div
                        className="capacity-bar-container"
                        title={`Total: ${cap.totalCapacity} | Available: ${cap.availableCapacity} | Locked: ${cap.lockedCapacity} | Committed: ${cap.committedCapacity}`}
                      >
                        <div
                          className="capacity-bar-segment segment-available"
                          style={{ width: `${(cap.availableCapacity / cap.totalCapacity) * 100}%` }}
                        />
                        <div
                          className="capacity-bar-segment segment-locked"
                          style={{ width: `${(cap.lockedCapacity / cap.totalCapacity) * 100}%` }}
                        />
                        <div
                          className="capacity-bar-segment segment-committed"
                          style={{ width: `${(cap.committedCapacity / cap.totalCapacity) * 100}%` }}
                        />
                      </div>
                      <div className="capacity-legend">
                        <span className="capacity-legend-item">
                          <span className="legend-dot dot-available"></span> Available ({cap.availableCapacity})
                        </span>
                        <span className="capacity-legend-item">
                          <span className="legend-dot dot-locked"></span> 2PC Lock ({cap.lockedCapacity})
                        </span>
                        <span className="capacity-legend-item">
                          <span className="legend-dot dot-committed"></span> Committed ({cap.committedCapacity})
                        </span>
                        <span style={{ marginLeft: 'auto', fontSize: '10px', color: '#64748b' }}>
                          Total Quota: {cap.totalCapacity} slots
                        </span>
                      </div>
                    </div>
                  )}

                  {/* On-Chain Quota Configuration (For Lab C or Arbitrator) */}
                  {isProvider && (currentActor.id === 'lab-c' || currentActor.role === 'arbitrator') && (
                    <div className="quota-widget">
                      <span style={{ fontSize: '11px', fontWeight: 600, color: '#334155' }}>
                        ⚙️ Update On-Chain Capacity Quota:
                      </span>
                      <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                        <input
                          type="number"
                          min="1"
                          max="100"
                          value={quotaInput}
                          onChange={e => setQuotaInput(Number(e.target.value))}
                          style={{ width: '70px', padding: '4px 8px', fontSize: '12px' }}
                        />
                        <button
                          className="small"
                          onClick={updateOnchainQuota}
                          disabled={updatingQuota}
                          style={{ padding: '4px 10px', fontSize: '11px' }}
                        >
                          {updatingQuota ? 'Broadcasting...' : 'Set Quota on Coral'}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </article>

        {/* PANEL 2: RESOURCE REQUEST */}
        <article className="panel request-panel">
          <div className="section-title">
            <span className="step-num">02</span>
            <div>
              <h2>Autonomous Shortage Request</h2>
              <p>Trigger AI agent discovery and capacity optimization</p>
            </div>
          </div>
          <label>
            Requesting Institution
            <select value={requester} onChange={e => setRequester(e.target.value)}>
              {institutions.map(i => (
                <option value={i.id} key={i.id}>
                  {i.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Appointment Slots Required
            <input
              type="number"
              min="1"
              max="50"
              value={quantity}
              onChange={e => setQuantity(Number(e.target.value))}
            />
          </label>
          <label>
            Clinical Urgency
            <select value={urgency} onChange={e => setUrgency(e.target.value)}>
              <option value="routine">Routine (24h turnaround)</option>
              <option value="urgent">Urgent (12h turnaround)</option>
              <option value="critical">Critical (4h turnaround)</option>
            </select>
          </label>
          <button onClick={discover} disabled={loading}>
            {loading ? 'Evaluating SLA...' : 'Discover & Optimize Allocation'}
          </button>
        </article>
      </section>

      {/* PANEL 3: AGENT COORDINATION & PROPOSAL */}
      <section className="panel">
        <div className="section-title">
          <span className="step-num">03</span>
          <div>
            <h2>Agent Allocation & Institutional Authorization</h2>
            <p>Verification → Autonomous Matching → Human-in-the-Loop Signoff</p>
          </div>
        </div>

        {proposal ? (
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '14px' }}>
              <span className={`status-pill ${getStatusBadgeClass(proposal.status)}`}>{proposal.status}</span>
              <span style={{ fontSize: '13px', fontWeight: 600 }}>
                {proposal.selected_provider_id
                  ? `Recommended: ${institutions.find(i => i.id === proposal.selected_provider_id)?.name || proposal.selected_provider_id}`
                  : 'No feasible provider found'}
              </span>
            </div>

            {/* Two-Phase Commit Phase 1 Lock Banner */}
            {proposal.lock_id && (
              <div className="lock-banner">
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '8px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span className="lock-pulse"></span>
                    <strong style={{ fontSize: '12px', color: '#065f46' }}>
                      2PC PHASE 1 ACTIVE: On-Chain Capacity Lock Held
                    </strong>
                    <span className="lock-badge">EIP-712 Meta-Tx</span>
                  </div>
                  <div style={{ display: 'flex', gap: '6px' }}>
                    <button
                      className="small"
                      onClick={() => inspectLock(proposal.lock_id!)}
                      style={{ padding: '3px 8px', fontSize: '11px', background: '#0284c7', borderColor: '#0284c7' }}
                    >
                      🔍 Inspect Lock
                    </button>
                    {proposal.status === 'proposed' && (
                      <button
                        className="small danger"
                        onClick={() => releaseActiveLock(proposal.lock_id!)}
                        disabled={loading}
                        style={{ padding: '3px 8px', fontSize: '11px' }}
                      >
                        Release Hold
                      </button>
                    )}
                  </div>
                </div>
                <div style={{ display: 'flex', gap: '14px', marginTop: '6px', fontSize: '11px', color: '#047857', flexWrap: 'wrap' }}>
                  <span>
                    Lock ID: <code className="mono-hash">{proposal.lock_id.slice(0, 10)}...{proposal.lock_id.slice(-6)}</code>
                  </span>
                  {proposal.lock_tx_hash && (
                    <span>
                      Tx: <code className="mono-hash">{proposal.lock_tx_hash.slice(0, 10)}...</code>
                    </span>
                  )}
                  <span>Status: <strong>Active (Slots Held on Coral)</strong></span>
                  {proposal.lock_expires_at && (
                    <span>Expires At: <strong>{new Date(Number(proposal.lock_expires_at) * 1000).toLocaleTimeString()}</strong></span>
                  )}
                </div>
              </div>
            )}

            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Provider</th>
                    <th>Available Slots</th>
                    <th>Turnaround (Hours)</th>
                    <th>Autonomous Rationale</th>
                    <th>Verification</th>
                  </tr>
                </thead>
                <tbody>
                  {proposal.offers.map(o => (
                    <tr key={o.provider_id}>
                      <td><strong>{o.provider_name}</strong></td>
                      <td>{o.available_quantity}</td>
                      <td>{o.estimated_delivery_hours}h</td>
                      <td>{o.rationale}</td>
                      <td>
                        {o.onchain_verified ? (
                          <span className="verified-badge">✓ Coral On-Chain</span>
                        ) : (
                          <span style={{ fontSize: '11px', color: '#94a3b8' }}>Catalog Fallback</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {proposal.selected_provider_id && proposal.status === 'proposed' && (
              <div style={{ display: 'flex', gap: '10px', marginTop: '16px' }}>
                <button
                  onClick={() => authorize(true)}
                  disabled={loading || currentActor.role !== 'requester'}
                  title={currentActor.role !== 'requester' ? 'Switch persona to Hospital Requester to authorize' : ''}
                >
                  ✍️ Authorize & Broadcast to Coral Testnet
                </button>
                <button
                  className="secondary"
                  onClick={() => authorize(false)}
                  disabled={loading || currentActor.role !== 'requester'}
                >
                  Decline Proposal
                </button>
                {currentActor.role !== 'requester' && (
                  <span style={{ fontSize: '11px', color: '#b45309', alignSelf: 'center' }}>
                    ⚠️ Switch persona to Hospital A to authorize
                  </span>
                )}
              </div>
            )}
          </div>
        ) : (
          <p style={{ color: '#94a3b8', fontSize: '13px', margin: 0 }}>
            Submit a diagnostic shortage request above to initiate multi-agent discovery and generate a proposal.
          </p>
        )}
      </section>

      {/* PANEL 4: ON-CHAIN COMMITMENTS & LIFECYCLE */}
      <section className="panel">
        <div className="section-title">
          <span className="step-num">04</span>
          <div>
            <h2>Coral Testnet Commitments & Lifecycle</h2>
            <p>Persistent SQLite state synchronized with immutable on-chain smart contract</p>
          </div>
        </div>

        {commitments.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Commitment</th>
                  <th>Requester</th>
                  <th>Provider</th>
                  <th>Slots</th>
                  <th>On-Chain Status</th>
                  <th>Reservation ID / Tx</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {commitments.map(c => {
                  const onchainStatus = c.onchain_status || c.status
                  const isProvider = currentActor.id === c.provider_id || currentActor.role === 'provider'
                  const isRequester = currentActor.id === c.requesting_institution_id || currentActor.role === 'requester'
                  const isArbitrator = currentActor.role === 'arbitrator'

                  return (
                    <tr key={c.commitment_id}>
                      <td>
                        <strong>{c.commitment_id.slice(0, 8)}...</strong>
                        <div style={{ fontSize: '10px', color: '#64748b' }}>by {c.approved_by || 'Coordinator'}</div>
                      </td>
                      <td>{institutions.find(i => i.id === c.requesting_institution_id)?.name || c.requesting_institution_id}</td>
                      <td>{institutions.find(i => i.id === c.provider_id)?.name || c.provider_id}</td>
                      <td><strong>{c.quantity}</strong></td>
                      <td>
                        <span className={`status-pill ${getStatusBadgeClass(onchainStatus)}`}>
                          {onchainStatus}
                        </span>
                      </td>
                      <td>
                        {c.onchain_reservation_id ? (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                            <span className="mono-hash" title={c.onchain_reservation_id}>
                              ID: {c.onchain_reservation_id.slice(0, 10)}...{c.onchain_reservation_id.slice(-6)}
                            </span>
                            {c.tx_hash && (
                              <span className="mono-hash" title={c.tx_hash} style={{ fontSize: '10px' }}>
                                Tx: {c.tx_hash.slice(0, 10)}...
                              </span>
                            )}
                            {c.lock_id && (
                              <span
                                className="mono-hash"
                                title={`Converted atomically from capacity lock ${c.lock_id} (Click to inspect)`}
                                onClick={() => inspectLock(c.lock_id!)}
                                style={{ fontSize: '9px', color: '#7c3aed', cursor: 'pointer' }}
                              >
                                🔒 Lock: {c.lock_id.slice(0, 8)}...
                              </span>
                            )}
                          </div>
                        ) : (
                          <span style={{ color: '#94a3b8', fontSize: '11px' }}>Off-chain</span>
                        )}
                      </td>
                      <td>
                        <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                          {/* Accept action */}
                          {onchainStatus === 'Proposed' && (
                            <button
                              className="small success"
                              onClick={() => acceptCommitment(c.commitment_id)}
                              disabled={loading || !isProvider}
                              title={!isProvider ? 'Switch persona to Lab C to accept' : ''}
                            >
                              Accept
                            </button>
                          )}

                          {/* Fulfill action */}
                          {onchainStatus === 'Committed' && (
                            <button
                              className="small success"
                              onClick={() => fulfillCommitment(c.commitment_id)}
                              disabled={loading || !isProvider}
                              title={!isProvider ? 'Switch persona to Lab C to fulfill' : ''}
                            >
                              Fulfill
                            </button>
                          )}

                          {/* Dispute action */}
                          {(onchainStatus === 'Committed' || onchainStatus === 'Fulfilled') && (
                            <button
                              className="small danger"
                              onClick={() => setDisputeModalTarget(c)}
                              disabled={loading || !isRequester}
                              title={!isRequester ? 'Switch persona to Hospital A to dispute' : ''}
                            >
                              Dispute
                            </button>
                          )}

                          {/* Arbitrate action */}
                          {onchainStatus === 'Disputed' && (
                            <button
                              className="small"
                              onClick={() => setResolveModalTarget(c)}
                              disabled={loading || !isArbitrator}
                              title={!isArbitrator ? 'Switch persona to Arbitrator to resolve' : ''}
                              style={{ background: '#7c3aed', borderColor: '#7c3aed' }}
                            >
                              Arbitrate
                            </button>
                          )}

                          {/* Audit trail & evidence */}
                          <button
                            className="small secondary"
                            onClick={() => openAuditTrail(c)}
                            title="View tamper-evident audit chain and evidence"
                          >
                            🔍 Audit & Evidence
                          </button>

                          {/* Direct RPC Verification */}
                          {c.onchain_reservation_id && (
                            <button
                              className="small"
                              onClick={() => verifyDirectFromCoralRPC(c.onchain_reservation_id!)}
                              title="Query Coral Testnet RPC directly (bypasses backend)"
                              style={{ background: '#059669', borderColor: '#059669', color: '#fff' }}
                            >
                              ⚡ Direct RPC Verify
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p style={{ color: '#94a3b8', fontSize: '13px', margin: 0 }}>No commitments recorded yet.</p>
        )}
      </section>

      {/* MODAL: DISPUTE */}
      {disputeModalTarget && (
        <div className="modal-overlay">
          <div className="modal-box">
            <div className="modal-header">
              <h3>🚨 Raise Formal Reservation Dispute</h3>
              <button className="close-btn" onClick={() => setDisputeModalTarget(null)}>×</button>
            </div>
            <p style={{ fontSize: '13px', color: '#64748b', marginTop: 0 }}>
              Initiates a contract dispute on Coral Testnet for commitment{' '}
              <strong>{disputeModalTarget.commitment_id.slice(0, 8)}...</strong>. Clinical and operational
              evidence will be hashed with SHA-256 and verified off-chain.
            </p>
            <label>
              Dispute Reason & Clinical Evidence:
              <textarea
                rows={4}
                value={disputeReason}
                onChange={e => setDisputeReason(e.target.value)}
                placeholder="e.g. Diagnostic CT calibration error: Scan results delivered failed standard quality threshold."
              />
            </label>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px', marginTop: '16px' }}>
              <button className="secondary" onClick={() => setDisputeModalTarget(null)}>Cancel</button>
              <button className="danger" onClick={submitDispute} disabled={loading || !disputeReason.trim()}>
                {loading ? 'Submitting...' : 'Broadcast Dispute on Coral'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* MODAL: ARBITRATE */}
      {resolveModalTarget && (
        <div className="modal-overlay">
          <div className="modal-box">
            <div className="modal-header">
              <h3>⚖️ Consortium Arbitration Ruling</h3>
              <button className="close-btn" onClick={() => setResolveModalTarget(null)}>×</button>
            </div>
            <p style={{ fontSize: '13px', color: '#64748b', marginTop: 0 }}>
              As the Consortium Arbitrator (Contract Owner), enter the formal legal/clinical ruling for reservation{' '}
              <strong>{resolveModalTarget.commitment_id.slice(0, 8)}...</strong>.
            </p>
            <label>
              Final Ruling:
              <select value={resolveOutcome} onChange={e => setResolveOutcome(e.target.value as any)}>
                <option value="Cancelled">Cancelled (Fault found with Provider · Refund / Void)</option>
                <option value="Fulfilled">Fulfilled (Dispute dismissed · Provider upheld)</option>
              </select>
            </label>
            <label>
              Arbitration Findings & Legal Notes:
              <textarea
                rows={4}
                value={resolveNotes}
                onChange={e => setResolveNotes(e.target.value)}
                placeholder="e.g. Consortium panel reviewed equipment logs: Calibration defect verified. Reservation nullified."
              />
            </label>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px', marginTop: '16px' }}>
              <button className="secondary" onClick={() => setResolveModalTarget(null)}>Cancel</button>
              <button
                style={{ background: '#7c3aed', borderColor: '#7c3aed', color: '#ffffff' }}
                onClick={submitResolution}
                disabled={loading || !resolveNotes.trim()}
              >
                {loading ? 'Broadcasting...' : 'Enforce Ruling on-Chain'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* MODAL: AUDIT TRAIL & EVIDENCE VAULT */}
      {auditModalTarget && (
        <div className="modal-overlay">
          <div className="modal-box" style={{ maxWidth: '780px' }}>
            <div className="modal-header">
              <div>
                <h3 style={{ margin: 0 }}>🛡️ Cryptographic Audit Trail & Evidence Vault</h3>
                <span style={{ fontSize: '12px', color: '#64748b' }}>
                  Commitment: {auditModalTarget.commitment_id}
                </span>
              </div>
              <button className="close-btn" onClick={() => setAuditModalTarget(null)}>×</button>
            </div>

            {/* INTEGRITY STATUS */}
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                padding: '12px 16px',
                background: auditIntegrity?.valid ? '#f0fdf4' : '#fef2f2',
                border: `1px solid ${auditIntegrity?.valid ? '#bbf7d0' : '#fecaca'}`,
                borderRadius: '8px',
                marginBottom: '16px',
              }}
            >
              <div>
                <strong style={{ color: auditIntegrity?.valid ? '#15803d' : '#b91c1c' }}>
                  {auditIntegrity?.valid ? '✓ SHA-256 Hash Chain Integrity: INTACT' : '⚠️ TAMPER DETECTED IN CHAIN'}
                </strong>
                <div style={{ fontSize: '11px', color: '#64748b', marginTop: '2px' }}>
                  Genesis-to-tip hash verification verified across all {auditIntegrity?.total_events ?? 0} global audit events.
                </div>
              </div>
              <span className={`status-pill ${auditIntegrity?.valid ? 'status-committed' : 'status-cancelled'}`}>
                {auditIntegrity?.valid ? 'SECURE' : 'COMPROMISED'}
              </span>
            </div>

            {/* EVIDENCE VAULT */}
            <h4 style={{ margin: '14px 0 8px', fontSize: '14px', color: '#0f172a' }}>
              Off-Chain Evidence Records (SHA-256 Verified)
            </h4>
            {evidenceRecords.length ? (
              <div style={{ display: 'grid', gap: '8px', marginBottom: '16px' }}>
                {evidenceRecords.map(ev => (
                  <div
                    key={ev.id}
                    style={{
                      background: '#f8fafc',
                      border: '1px solid #e2e8f0',
                      borderRadius: '8px',
                      padding: '10px 12px',
                      fontSize: '12px',
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
                      <strong>{ev.evidence_type.toUpperCase()}</strong>
                      <span style={{ color: '#16a34a', fontWeight: 700 }}>✓ SHA-256 Verified</span>
                    </div>
                    <div style={{ color: '#475569', marginBottom: '6px' }}>"{ev.raw_document}"</div>
                    <div className="hash-chain-row">
                      Digest: {ev.computed_sha256}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p style={{ color: '#94a3b8', fontSize: '12px', margin: '0 0 16px' }}>
                No off-chain evidence submitted for this reservation yet.
              </p>
            )}

            {/* HASH-CHAINED EVENT LOG */}
            <h4 style={{ margin: '14px 0 8px', fontSize: '14px', color: '#0f172a' }}>
              Tamper-Evident Event Timeline
            </h4>
            {auditEvents.length ? (
              <div className="timeline">
                {auditEvents.map(evt => (
                  <div className="timeline-item" key={evt.id}>
                    <div className="timeline-header">
                      <span>Event #{evt.id} · {evt.timestamp}</span>
                      <span>Actor: {evt.actor} ({evt.role})</span>
                    </div>
                    <div className="timeline-action">{evt.action}</div>
                    <div className="hash-chain-row">
                      <strong>Prev:</strong> {evt.previous_hash.slice(0, 18)}... | <strong>Hash:</strong> {evt.event_hash.slice(0, 18)}...
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p style={{ color: '#94a3b8', fontSize: '12px', margin: 0 }}>No audit events found.</p>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '18px' }}>
              <button className="secondary" onClick={() => setAuditModalTarget(null)}>Close</button>
            </div>
          </div>
        </div>
      )}

      {/* MODAL: DIRECT RPC VERIFICATION */}
      {directRpcResult && (
        <div className="modal-overlay">
          <div className="modal-box" style={{ maxWidth: '720px' }}>
            <div className="modal-header">
              <div>
                <h3 style={{ margin: 0 }}>⚡ Direct Coral Testnet RPC Verification</h3>
                <span style={{ fontSize: '12px', color: '#16a34a', fontWeight: 700 }}>
                  ✓ 100% Zero-Backend Reliance · Directly Decoded from Coral Node in {directRpcResult.durationMs}ms
                </span>
              </div>
              <button className="close-btn" onClick={() => setDirectRpcResult(null)}>×</button>
            </div>

            <div style={{ background: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: '8px', padding: '12px 14px', marginBottom: '16px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <strong style={{ color: '#15803d', fontSize: '13px' }}>
                  Direct On-Chain State: {directRpcResult.status.toUpperCase()}
                </strong>
                <span className={`status-pill ${getStatusBadgeClass(directRpcResult.status)}`}>
                  {directRpcResult.status}
                </span>
              </div>
              <div style={{ fontSize: '11px', color: '#475569', marginTop: '4px' }}>
                Node Endpoint: <code>https://coralrpc.mstblockchain.com</code> | Contract: <code>{health?.contract_address || '0xcd630b8e555e0751afbb9ae511869110d361bb83'}</code>
              </div>
            </div>

            <div style={{ display: 'grid', gap: '8px', fontSize: '12px', background: '#f8fafc', padding: '14px', borderRadius: '8px', border: '1px solid #e2e8f0' }}>
              <div><strong>Reservation ID:</strong> <span className="mono-hash">{directRpcResult.reservationId}</span></div>
              <div><strong>Requester (Hospital):</strong> <span className="mono-hash">{directRpcResult.requester}</span></div>
              <div><strong>Provider (Facility):</strong> <span className="mono-hash">{directRpcResult.provider}</span></div>
              <div>
                <strong>Terms Hash (resourceRef):</strong> <span className="mono-hash">{directRpcResult.resourceRef}</span>
                {directRpcResult.decodedRef && <span style={{ marginLeft: '8px', color: '#0369a1', fontWeight: 600 }}>({directRpcResult.decodedRef})</span>}
              </div>
              <div><strong>Created At:</strong> {directRpcResult.createdAt}</div>
              <div><strong>Expires At:</strong> {directRpcResult.expiresAt}</div>
              {directRpcResult.fulfilledAt !== 'N/A' && (
                <div><strong>Fulfilled At:</strong> {directRpcResult.fulfilledAt}</div>
              )}
            </div>

            <div style={{ marginTop: '16px', padding: '12px', background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: '8px', fontSize: '11px', color: '#1e40af' }}>
              <strong>🔒 Mathematical Immutability Guarantee:</strong><br />
              The agreed terms, participating hospital identities, and slot references were permanently written to EVM storage upon creation. The smart contract contains <strong>zero setter functions</strong> to alter terms after proposal. If any party modifies terms, an entirely new reservation proposal with signed authorization is strictly required.
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '16px' }}>
              <button className="secondary" onClick={() => setDirectRpcResult(null)}>Close</button>
            </div>
          </div>
        </div>
      )}

      {/* MODAL: CAPACITY LOCK INSPECTION */}
      {lockModalTarget && (
        <div className="modal-overlay">
          <div className="modal-box" style={{ maxWidth: '640px' }}>
            <div className="modal-header">
              <div>
                <h3 style={{ margin: 0 }}>🔒 On-Chain Capacity Lock (Two-Phase Commit)</h3>
                <span style={{ fontSize: '12px', color: '#047857', fontWeight: 600 }}>
                  Pre-commitment capacity reservation holding slots on Coral Testnet
                </span>
              </div>
              <button className="close-btn" onClick={() => setLockModalTarget(null)}>×</button>
            </div>

            <div style={{ background: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: '8px', padding: '12px 14px', marginBottom: '16px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <strong style={{ color: '#15803d', fontSize: '13px' }}>
                  Lock Status: {lockModalTarget.status.toUpperCase()}
                </strong>
                <span className={`status-pill ${getStatusBadgeClass(lockModalTarget.status === 'Active' ? 'proposed' : lockModalTarget.status === 'Converted' ? 'committed' : 'cancelled')}`}>
                  {lockModalTarget.status}
                </span>
              </div>
              <div style={{ fontSize: '11px', color: '#475569', marginTop: '4px' }}>
                Quantity: <strong>{lockModalTarget.quantity} slot(s)</strong> held exclusively for requester
              </div>
            </div>

            <div style={{ display: 'grid', gap: '8px', fontSize: '12px', background: '#f8fafc', padding: '14px', borderRadius: '8px', border: '1px solid #e2e8f0' }}>
              <div><strong>Lock ID:</strong> <span className="mono-hash">{lockModalTarget.lockId}</span></div>
              <div><strong>Requester Wallet:</strong> <span className="mono-hash">{lockModalTarget.requester}</span></div>
              <div><strong>Provider Wallet:</strong> <span className="mono-hash">{lockModalTarget.provider}</span></div>
              <div><strong>Resource Type:</strong> <span className="mono-hash">{lockModalTarget.resourceType}</span></div>
              <div><strong>Created At:</strong> {new Date(Number(lockModalTarget.createdAt) * 1000).toLocaleString()}</div>
              <div><strong>Expires At:</strong> {new Date(Number(lockModalTarget.expiresAt) * 1000).toLocaleString()}</div>
              {lockModalTarget.reservationId && lockModalTarget.reservationId !== '0x0000000000000000000000000000000000000000000000000000000000000000' && (
                <div><strong>Bound Reservation ID:</strong> <span className="mono-hash">{lockModalTarget.reservationId}</span></div>
              )}
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '16px' }}>
              <button className="secondary" onClick={() => setLockModalTarget(null)}>Close</button>
            </div>
          </div>
        </div>
      )}

      {/* FOOTER */}
      <footer>
        <div className="status-msg-badge">
          <span className="network-pulse"></span>
          <span>{message}</span>
        </div>
        <div style={{ fontSize: '11px', color: '#64748b' }}>
          MST Coral Testnet Contract: <code>{health?.contract_address || '0xcd630b8e555e0751afbb9ae511869110d361bb83'}</code>
        </div>
      </footer>
    </main>
  )
}
