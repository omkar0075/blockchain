from uuid import uuid4
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .agents import discover_and_verify, choose_offer
from .blockchain import (
    get_blockchain_status,
    create_onchain_reservation,
    accept_onchain_reservation,
    fulfill_onchain_reservation,
    dispute_onchain_reservation,
    resolve_onchain_dispute,
    get_onchain_reservation,
)
from .data import INSTITUTIONS, PROPOSALS, COMMITMENTS
from .models import (
    ResourceRequest,
    AllocationProposal,
    AuthorizationRequest,
    DisputeRequest,
    ResolveDisputeRequest,
)

app = FastAPI(
    title="MEDSWARM API",
    description="Autonomous healthcare resource coordination with Coral Testnet on-chain verification.",
    version="0.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sync_commitment_from_chain(commitment: dict) -> None:
    """Synchronize Python in-memory commitment dictionary with live Coral contract state."""
    res_id = commitment.get("onchain_reservation_id")
    if not res_id:
        return

    data = get_onchain_reservation(res_id)
    if not data.get("success") or not data.get("exists"):
        return

    res = data["reservation"]
    onchain_status_name = res.get("status")
    commitment["onchain_status"] = onchain_status_name
    commitment["onchain_status_code"] = res.get("statusCode")
    commitment["fulfilled_at"] = res.get("fulfilledAt")
    commitment["dispute_evidence"] = res.get("disputeEvidence")
    commitment["disputed_by"] = res.get("disputedBy")

    # Map on-chain status to Python commitment status
    if onchain_status_name == "Committed":
        commitment["status"] = "committed"
    elif onchain_status_name == "Fulfilled":
        commitment["status"] = "fulfilled"
    elif onchain_status_name == "Disputed":
        commitment["status"] = "disputed"
    elif onchain_status_name == "Cancelled":
        commitment["status"] = "cancelled"
    elif onchain_status_name == "Expired":
        commitment["status"] = "expired"


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
        _sync_commitment_from_chain(commitment)
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
    # Sync each active commitment with latest on-chain state
    for commitment in COMMITMENTS.values():
        _sync_commitment_from_chain(commitment)
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

    commitment["status"] = "committed"
    commitment["onchain_acceptance_tx_hash"] = accept_res["txHash"]
    commitment["acceptance_block"] = accept_res["blockNumber"]
    _sync_commitment_from_chain(commitment)
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
    _sync_commitment_from_chain(commitment)
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

    _sync_commitment_from_chain(commitment)
    current_status = commitment.get("status")
    onchain_status = commitment.get("onchain_status")

    if current_status == "fulfilled":
        raise HTTPException(status_code=409, detail="Commitment is already fulfilled")

    # In on-chain mode, must be Committed before it can be fulfilled
    if onchain_status and onchain_status != "Committed":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot fulfill reservation with on-chain status '{onchain_status}'. Reservation must be 'Committed' by provider first.",
        )

    reservation_id = commitment.get("onchain_reservation_id")
    provider_id = commitment.get("provider_id", "lab-c")

    if reservation_id:
        fulfill_res = fulfill_onchain_reservation(reservation_id, provider=provider_id)
        if not fulfill_res.get("success"):
            raise HTTPException(
                status_code=502,
                detail=f"On-chain fulfillment failed: {fulfill_res.get('error')}",
            )

        commitment["onchain_fulfillment_tx_hash"] = fulfill_res["txHash"]
        commitment["fulfillment_block"] = fulfill_res["blockNumber"]
        commitment["relayer_topup_tx"] = fulfill_res.get("relayerTopUpTx")
        commitment["note"] = (
            "Fulfilled on Coral Testnet by provider. 7-day dispute window (604,800s) is now active."
        )
        _sync_commitment_from_chain(commitment)
        return {
            "status": "fulfilled",
            "commitment": commitment,
            "blockchain": fulfill_res,
        }
    else:
        # Off-chain mock fallback
        commitment["status"] = "fulfilled"
        commitment["note"] = "Fulfilled in off-chain demo ledger."
        return {
            "status": "fulfilled",
            "commitment": commitment,
        }


@app.post("/commitments/{commitment_id}/dispute")
def dispute_commitment(commitment_id: str, dispute: DisputeRequest):
    commitment = COMMITMENTS.get(commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")

    _sync_commitment_from_chain(commitment)
    onchain_status = commitment.get("onchain_status")

    if onchain_status not in ["Committed", "Fulfilled"]:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot dispute reservation in '{onchain_status}' status. Must be Committed or Fulfilled.",
        )

    reservation_id = commitment.get("onchain_reservation_id")
    if not reservation_id:
        raise HTTPException(status_code=400, detail="Commitment has no on-chain reservation ID.")

    dispute_res = dispute_onchain_reservation(
        reservation_id=reservation_id,
        actor=dispute.disputed_by,
        reason=dispute.reason,
        evidence_hash=dispute.evidence_hash,
    )

    if not dispute_res.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"On-chain dispute initiation failed: {dispute_res.get('error')}",
        )

    commitment["dispute_tx_hash"] = dispute_res["txHash"]
    commitment["dispute_block"] = dispute_res["blockNumber"]
    commitment["dispute_reason"] = dispute.reason
    commitment["evidence_hash"] = dispute_res["evidenceHash"]
    _sync_commitment_from_chain(commitment)

    return {
        "status": "disputed",
        "commitment": commitment,
        "blockchain": dispute_res,
    }


@app.post("/commitments/{commitment_id}/resolve")
def resolve_dispute(commitment_id: str, resolution: ResolveDisputeRequest):
    commitment = COMMITMENTS.get(commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")

    _sync_commitment_from_chain(commitment)
    onchain_status = commitment.get("onchain_status")

    if onchain_status != "Disputed":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot resolve reservation with status '{onchain_status}'. Reservation must be 'Disputed'.",
        )

    reservation_id = commitment.get("onchain_reservation_id")
    if not reservation_id:
        raise HTTPException(status_code=400, detail="Commitment has no on-chain reservation ID.")

    resolve_res = resolve_onchain_dispute(
        reservation_id=reservation_id,
        resolution=resolution.resolution,
        notes=resolution.notes,
        resolution_evidence=resolution.resolution_evidence,
    )

    if not resolve_res.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"On-chain dispute resolution failed: {resolve_res.get('error')}",
        )

    commitment["resolution_tx_hash"] = resolve_res["txHash"]
    commitment["resolution_block"] = resolve_res["blockNumber"]
    commitment["resolution_notes"] = resolution.notes
    commitment["resolution_evidence"] = resolve_res["resolutionEvidence"]
    _sync_commitment_from_chain(commitment)

    return {
        "status": "resolved",
        "commitment": commitment,
        "blockchain": resolve_res,
    }
