from uuid import uuid4
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .agents import discover_and_verify, choose_offer
from .data import INSTITUTIONS, PROPOSALS, COMMITMENTS
from .models import ResourceRequest, AllocationProposal, AuthorizationRequest

app = FastAPI(
    title="MEDSWARM API",
    description="Synthetic-data MVP for autonomous healthcare resource coordination.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok", "mode": "synthetic-demo", "blockchain": "not-connected"}

@app.get("/institutions")
def list_institutions():
    return INSTITUTIONS

@app.post("/requests", response_model=AllocationProposal)
def create_request(request: ResourceRequest):
    if not any(i["id"] == request.requesting_institution_id for i in INSTITUTIONS):
        raise HTTPException(status_code=404, detail="Requesting institution not found")

    offers = discover_and_verify(request)
    selected = choose_offer(request, offers)
    proposal = AllocationProposal(
        request=request,
        offers=offers,
        selected_provider_id=selected.provider_id if selected else None,
        selected_quantity=request.quantity if selected else 0,
        status="proposed",
    )
    proposal_id = str(uuid4())
    payload = proposal.model_dump()
    payload["proposal_id"] = proposal_id
    PROPOSALS[proposal_id] = payload
    return payload

@app.post("/proposals/{proposal_id}/authorize")
def authorize_proposal(proposal_id: str, approval: AuthorizationRequest):
    proposal = PROPOSALS.get(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if not approval.approved:
        proposal["status"] = "cancelled"
        return {"proposal_id": proposal_id, "status": "cancelled", "message": "Institution declined proposal"}
    if not proposal.get("selected_provider_id"):
        raise HTTPException(status_code=409, detail="No feasible provider offer to authorize")

    # Demo-only authorization: production must authenticate the approver and enforce role/policy checks.
    proposal["status"] = "authorized"
    commitment_id = str(uuid4())
    commitment = {
        "commitment_id": commitment_id,
        "proposal_id": proposal_id,
        "requesting_institution_id": proposal["request"]["requesting_institution_id"],
        "provider_id": proposal["selected_provider_id"],
        "resource_type": proposal["request"]["resource_type"],
        "quantity": proposal["selected_quantity"],
        "status": "authorized",
        "approved_by": approval.approved_by,
        "ledger": "in-memory-demo-only",
    }
    COMMITMENTS[commitment_id] = commitment
    return {"proposal_id": proposal_id, "status": "authorized", "commitment": commitment}

@app.get("/commitments")
def list_commitments():
    return list(COMMITMENTS.values())

@app.post("/commitments/{commitment_id}/fulfill")
def fulfill_commitment(commitment_id: str):
    commitment = COMMITMENTS.get(commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")
    if commitment["status"] != "authorized":
        raise HTTPException(status_code=409, detail="Only authorized commitments can be fulfilled")
    commitment["status"] = "fulfilled"
    return {
        "commitment": commitment,
        "note": "Demo status only; production fulfillment requires trusted evidence and authorized attestation."
    }
