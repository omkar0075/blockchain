from uuid import uuid4
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .agents import discover_and_verify, choose_offer
from .blockchain import (
    get_blockchain_status,
    create_onchain_reservation,
    accept_onchain_reservation,
    get_onchain_reservation,
)
from .data import INSTITUTIONS, PROPOSALS, COMMITMENTS
from .models import ResourceRequest, AllocationProposal, AuthorizationRequest

app = FastAPI(
    title="MEDSWARM API",
    description="Autonomous healthcare resource coordination with Coral Testnet on-chain verification.",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    bc = get_blockchain_status()
    bc_connected = bool(bc.get("connected"))
    relayer_info = bc.get("relayer", {})
    return {
        "status": "ok",
        "mode": "hybrid-autonomous-coordination",
        "blockchain": {
            "status": "connected" if bc_connected else "disconnected",
            "network": bc.get("network", "Coral Testnet"),
            "chain_id": bc.get("chainId", 54635),
            "contract_address": bc.get("contractAddress"),
            "latest_block": bc.get("latestBlock"),
            "relayer_configured": relayer_info.get("configured", False),
            "relayer_address": relayer_info.get("address"),
            "institutions_registered": list(bc.get("registeredInstitutions", {}).keys()),
        },
    }

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

    proposal["status"] = "authorized"
    commitment_id = str(uuid4())
    requester_id = proposal["request"]["requesting_institution_id"]
    provider_id = proposal["selected_provider_id"]
    resource_ref = f"medway-{proposal_id[:8]}"

    # Attempt to record commitment on Coral Testnet via the Viem bridge
    onchain_res = create_onchain_reservation(
        requester=requester_id,
        provider=provider_id,
        resource_ref=resource_ref,
    )

    commitment = {
        "commitment_id": commitment_id,
        "proposal_id": proposal_id,
        "requesting_institution_id": requester_id,
        "provider_id": provider_id,
        "resource_type": proposal["request"]["resource_type"],
        "quantity": proposal["selected_quantity"],
        "status": "authorized",
        "approved_by": approval.approved_by,
    }

    if onchain_res.get("success"):
        commitment["ledger"] = "coral-testnet"
        commitment["onchain_reservation_id"] = onchain_res["reservationId"]
        commitment["tx_hash"] = onchain_res["txHash"]
        commitment["block_number"] = onchain_res["blockNumber"]
        commitment["onchain_status"] = "Proposed"
        commitment["message"] = f"Commitment registered on Coral Testnet in block #{onchain_res['blockNumber']}"
    else:
        commitment["ledger"] = "coral-testnet-ready"
        commitment["relayer_notice"] = onchain_res.get("error")
        commitment["message"] = (
            "Authorized by institution. To broadcast live to Coral Testnet, ensure "
            "CORAL_PRIVATE_KEY is set in backend/.env."
        )

    COMMITMENTS[commitment_id] = commitment
    return {"proposal_id": proposal_id, "status": "authorized", "commitment": commitment}

@app.get("/commitments")
def list_commitments():
    return list(COMMITMENTS.values())

@app.post("/commitments/{commitment_id}/accept")
def accept_commitment(commitment_id: str):
    commitment = COMMITMENTS.get(commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")

    reservation_id = commitment.get("onchain_reservation_id")
    if not reservation_id:
        raise HTTPException(
            status_code=400,
            detail="Commitment does not have an on-chain reservation ID to accept.",
        )

    provider_id = commitment.get("provider_id", "lab-c")
    accept_res = accept_onchain_reservation(reservation_id, provider=provider_id)

    if not accept_res.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"Coral acceptance failed: {accept_res.get('error')}",
        )

    commitment["onchain_acceptance_tx_hash"] = accept_res["txHash"]
    commitment["onchain_status"] = "Committed"
    commitment["acceptance_block"] = accept_res["blockNumber"]
    return {
        "status": "committed",
        "commitment": commitment,
        "blockchain": accept_res,
    }

@app.get("/commitments/{commitment_id}/onchain")
def get_commitment_onchain(commitment_id: str):
    commitment = COMMITMENTS.get(commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")

    reservation_id = commitment.get("onchain_reservation_id")
    if not reservation_id:
        return {
            "onchain": False,
            "commitment_id": commitment_id,
            "message": "Commitment has not been submitted on-chain.",
        }

    onchain_data = get_onchain_reservation(reservation_id)
    return {
        "onchain": True,
        "commitment_id": commitment_id,
        "reservation_id": reservation_id,
        "contract_state": onchain_data,
    }

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
        "note": "Fulfillment recorded in coordination ledger; real-world delivery attested off-chain."
    }
