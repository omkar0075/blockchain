import { useEffect, useState } from 'react'

const API = 'http://127.0.0.1:8000'

type Institution = { id: string; name: string; type: string; resources: Record<string, number> }
type Offer = { provider_id: string; provider_name: string; available_quantity: number; estimated_delivery_hours: number; rationale: string }
type Proposal = {
  proposal_id?: string
  request: { requesting_institution_id: string; quantity: number; urgency: string }
  offers: Offer[]
  selected_provider_id: string | null
  selected_quantity: number
  status: string
}
type Commitment = { commitment_id: string; provider_id: string; requesting_institution_id: string; quantity: number; status: string; ledger: string }

export default function App() {
  const [institutions, setInstitutions] = useState<Institution[]>([])
  const [requester, setRequester] = useState('hospital-a')
  const [quantity, setQuantity] = useState(3)
  const [urgency, setUrgency] = useState('urgent')
  const [proposal, setProposal] = useState<Proposal | null>(null)
  const [commitments, setCommitments] = useState<Commitment[]>([])
  const [message, setMessage] = useState('Demo mode: synthetic data; MST Blockchain is not connected yet.')

  async function load() {
    try {
      const [i, c] = await Promise.all([
        fetch(`${API}/institutions`).then(r => r.json()),
        fetch(`${API}/commitments`).then(r => r.json())
      ])
      setInstitutions(i); setCommitments(c)
    } catch {
      setMessage('Backend not reachable. Start FastAPI at http://127.0.0.1:8000.')
    }
  }

  useEffect(() => { load() }, [])

  async function discover() {
    setMessage('Discovering provider capacity and calculating a feasible proposal…')
    setProposal(null)
    try {
      const response = await fetch(`${API}/requests`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          requesting_institution_id: requester,
          resource_type: 'diagnostic_slot',
          quantity,
          urgency,
          required_by_hours: urgency === 'critical' ? 4 : urgency === 'urgent' ? 12 : 24
        })
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Request failed')
      setProposal(data)
      setMessage(data.selected_provider_id ? 'Proposal generated. Institutional authorization is required before commitment.' : 'No provider currently satisfies the requested quantity and time constraints.')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Could not create request.')
    }
  }

  async function authorize(approved: boolean) {
    if (!proposal?.proposal_id) return
    const approver = window.prompt('Enter demo approver name (prototype only):', 'Hospital Coordinator')
    if (!approver) return
    try {
      const response = await fetch(`${API}/proposals/${proposal.proposal_id}/authorize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved_by: approver, approved })
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Authorization failed')
      setMessage(data.message || 'Authorized. Demo commitment recorded in the in-memory ledger, not on blockchain.')
      await load()
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Authorization failed.')
    }
  }

  async function fulfill(id: string) {
    try {
      const response = await fetch(`${API}/commitments/${id}/fulfill`, { method: 'POST' })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Fulfillment update failed')
      setMessage('Fulfillment status updated in demo ledger; trusted evidence is not implemented yet.')
      await load()
    } catch (e) {
      setMessage(e instanceof Error ? e.message : 'Fulfillment update failed.')
    }
  }

  return <main className="shell">
    <header className="topbar">
      <div><div className="eyebrow">AUTONOMOUS HEALTHCARE COORDINATION</div><h1>MEDSWARM</h1><p>AI agents coordinate. Institutions authorize. Blockchain integration comes next.</p></div>
      <span className="badge">MVP · SYNTHETIC DATA</span>
    </header>

    <section className="notice"><strong>Prototype status:</strong> Resource discovery and authorization flow are simulated. This version does not connect to MST Blockchain and must not be used with real patient data.</section>

    <section className="grid">
      <article className="panel">
        <div className="section-title"><span>01</span><div><h2>Institution Network</h2><p>Simulated participating providers</p></div></div>
        <div className="institution-list">{institutions.map(i => <div className="institution" key={i.id}><div><strong>{i.name}</strong><small>{i.type.replace('_', ' ')}</small></div><span>{i.resources.diagnostic_slot} slots</span></div>)}</div>
      </article>

      <article className="panel request-panel">
        <div className="section-title"><span>02</span><div><h2>Resource Request</h2><p>Submit a diagnostic-capacity shortage</p></div></div>
        <label>Requesting institution<select value={requester} onChange={e => setRequester(e.target.value)}>{institutions.map(i => <option value={i.id} key={i.id}>{i.name}</option>)}</select></label>
        <label>Appointment slots<input type="number" min="1" max="100" value={quantity} onChange={e => setQuantity(Number(e.target.value))}/></label>
        <label>Urgency<select value={urgency} onChange={e => setUrgency(e.target.value)}><option value="routine">Routine</option><option value="urgent">Urgent</option><option value="critical">Critical</option></select></label>
        <button onClick={discover}>Discover & Optimize</button>
      </article>
    </section>

    <section className="panel">
      <div className="section-title"><span>03</span><div><h2>Agent Coordination</h2><p>Discovery → verification → proposed allocation → institutional approval</p></div></div>
      {proposal ? <>
        <div className="status-line"><span className="pill">{proposal.status}</span><span>{proposal.selected_provider_id ? `Proposed provider: ${institutions.find(i => i.id === proposal.selected_provider_id)?.name || proposal.selected_provider_id}` : 'No feasible provider selected'}</span></div>
        <h3>Provider offers</h3>
        {proposal.offers.length ? <div className="table-wrap"><table><thead><tr><th>Provider</th><th>Available slots</th><th>Estimated hours</th><th>Verification note</th></tr></thead><tbody>{proposal.offers.map(o => <tr key={o.provider_id}><td>{o.provider_name}</td><td>{o.available_quantity}</td><td>{o.estimated_delivery_hours}</td><td>{o.rationale}</td></tr>)}</tbody></table></div> : <p>No offers received.</p>}
        {proposal.selected_provider_id && proposal.status === 'proposed' && <div className="actions"><button onClick={() => authorize(true)}>Authorize proposal</button><button className="secondary" onClick={() => authorize(false)}>Decline</button></div>}
      </> : <p className="empty">Submit a request to see provider offers and the proposed allocation.</p>}
    </section>

    <section className="panel">
      <div className="section-title"><span>04</span><div><h2>Commitments</h2><p>Current prototype ledger · not on-chain</p></div></div>
      {commitments.length ? <div className="table-wrap"><table><thead><tr><th>Commitment</th><th>Requester</th><th>Provider</th><th>Quantity</th><th>Status</th><th>Action</th></tr></thead><tbody>{commitments.map(c => <tr key={c.commitment_id}><td>{c.commitment_id.slice(0, 8)}…</td><td>{c.requesting_institution_id}</td><td>{c.provider_id}</td><td>{c.quantity}</td><td><span className="pill">{c.status}</span></td><td>{c.status === 'authorized' && <button className="small" onClick={() => fulfill(c.commitment_id)}>Mark fulfilled</button>}</td></tr>)}</tbody></table></div> : <p className="empty">No commitments yet.</p>}
    </section>
    <footer><span className="live-dot"></span>{message}<span className="footer-note">MST network connection: pending verification</span></footer>
  </main>
}
