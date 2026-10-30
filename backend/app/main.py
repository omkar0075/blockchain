import hashlib
from uuid import uuid4
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

from .agents import discover_and_verify, choose_offer
from .auth import (
    ActorIdentity,
    authenticate_actor,
    sync_arbitrator_with_contract_owner,
    verify_authorization_permission,
    verify_acceptance_permission,
    verify_fulfillment_permission,
    verify_dispute_permission,
    verify_arbitration_permission,
)
from .blockchain import (
    get_blockchain_status,
    create_onchain_reservation,
    accept_onchain_reservation,
    fulfill_onchain_reservation,
    dispute_onchain_reservation,
    resolve_onchain_dispute,
    get_onchain_reservation,
)
from .data import INSTITUTIONS
from .db import (
    save_proposal,
    get_proposal,
    save_commitment,
    get_commitment,
    list_commitments as db_list_commitments,
    store_evidence,
    get_evidence_records,
    record_audit_event,
    get_audit_trail,
    verify_audit_trail_integrity,
)
from .models import (
    ResourceRequest,
    AllocationProposal,
    AuthorizationRequest,
    DisputeRequest,
    ResolveDisputeRequest,
)

app = FastAPI(
    title="MEDway API",
    description="Autonomous healthcare resource coordination with Coral Testnet on-chain verification and tamper-evident audit trails.",
    version="0.5.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sync_commitment_from_chain(commitment: dict) -> None:
    """Synchronize Python commitment with live Coral contract state and persist changes."""
    res_id = commitment.get("onchain_reservation_id")
    if not res_id:
        return

    data = get_onchain_reservation(res_id)
    if not data.get("success") or not data.get("exists"):
        return

    res = data["reservation"]
    onchain_status_name = res.get("status")
    changed = False

    if commitment.get("onchain_status") != onchain_status_name:
        commitment["onchain_status"] = onchain_status_name
        commitment["onchain_status_code"] = res.get("statusCode")
        changed = True

    if res.get("fulfilledAt") and commitment.get("fulfilled_at") != res.get("fulfilledAt"):
        commitment["fulfilled_at"] = res.get("fulfilledAt")
        changed = True

    if res.get("disputeEvidence") and commitment.get("dispute_evidence") != res.get("disputeEvidence"):
        commitment["dispute_evidence"] = res.get("disputeEvidence")
        commitment["disputed_by"] = res.get("disputedBy")
        changed = True

    status_map = {
        "Committed": "committed",
        "Fulfilled": "fulfilled",
        "Disputed": "disputed",
        "Cancelled": "cancelled",
        "Expired": "expired",
    }
    mapped_status = status_map.get(onchain_status_name)
    if mapped_status and commitment.get("status") != mapped_status:
        commitment["status"] = mapped_status
        changed = True

    if changed:
        save_commitment(commitment)


@app.get("/health")
def health():
    bc = get_blockchain_status()
    bc_connected = bool(bc.get("connected"))
    relayer_info = bc.get("relayer", {})
    contract_owner = bc.get("contractOwner")

    # Keep backend arbitrator address strictly synchronized with on-chain owner
    if contract_owner:
        sync_arbitrator_with_contract_owner(contract_owner)

    return {
        "status": "ok",
        "mode": "hybrid-autonomous-coordination",
        "database": "sqlite-persistent",
        "blockchain": {
            "status": "connected" if bc_connected else "disconnected",
            "network": bc.get("network", "Coral Testnet"),
            "chain_id": bc.get("chainId", 54635),
            "contract_address": bc.get("contractAddress"),
            "contract_owner": contract_owner,
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
def create_request(
    request: ResourceRequest,
    x_actor_id: Optional[str] = Header(None, alias="X-Actor-Id"),
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
):
    if not any(i["id"] == request.requesting_institution_id for i in INSTITUTIONS):
        raise HTTPException(status_code=404, detail="Requesting institution not found")

    actor = authenticate_actor(
        x_actor_id=x_actor_id,
        x_api_key=x_api_key,
        default_actor_id=request.requesting_institution_id,
    )

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

    # Persist in SQLite
    save_proposal(payload)

    # Record tamper-evident audit event
    record_audit_event(
        action="REQUEST_CREATED",
        actor=actor.id,
        role=actor.role,
        details={
            "proposal_id": proposal_id,
            "requesting_institution_id": request.requesting_institution_id,
            "resource_type": request.resource_type,
            "quantity": request.quantity,
            "urgency": request.urgency,
            "selected_provider_id": payload.get("selected_provider_id"),
        },
    )

    return payload


@app.post("/proposals/{proposal_id}/authorize")
def authorize_proposal(
    proposal_id: str,
    approval: AuthorizationRequest,
    x_actor_id: Optional[str] = Header(None, alias="X-Actor-Id"),
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
):
    proposal = get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    requester_id = proposal["request"]["requesting_institution_id"]

    # RBAC Guard: authenticate actor and verify credentials
    actor = authenticate_actor(
        x_actor_id=x_actor_id,
        x_api_key=x_api_key,
        default_actor_id=requester_id,
    )
    verify_authorization_permission(actor, requester_id)

    if not approval.approved:
        proposal["status"] = "cancelled"
        save_proposal(proposal)
        record_audit_event(
            action="PROPOSAL_DECLINED",
            actor=actor.id,
            role=actor.role,
            details={"proposal_id": proposal_id, "approved_by": approval.approved_by},
        )
        return {"proposal_id": proposal_id, "status": "cancelled", "message": "Institution declined proposal"}

    if not proposal.get("selected_provider_id"):
        raise HTTPException(status_code=409, detail="No feasible provider offer to authorize")

    proposal["status"] = "authorized"
    save_proposal(proposal)

    commitment_id = str(uuid4())
    provider_id = proposal["selected_provider_id"]

    # Compute deterministic canonical terms hash to guarantee terms immutability on-chain
    terms_payload = {
        "proposal_id": proposal_id,
        "requester": requester_id,
        "provider": provider_id,
        "resource_type": proposal["request"]["resource_type"],
        "quantity": proposal["selected_quantity"],
        "urgency": proposal["request"].get("urgency", "urgent"),
    }
    canonical_terms_str = json.dumps(terms_payload, sort_keys=True, separators=(",", ":"))
    terms_hash = "0x" + hashlib.sha256(canonical_terms_str.encode("utf-8")).hexdigest()
    resource_ref = terms_hash

    # Attempt on-chain reservation proposal on Coral Testnet via the Viem bridge
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
        "terms_hash": terms_hash,
        "canonical_terms": terms_payload,
        "status": "authorized",
        "approved_by": approval.approved_by,
        "created_at": None,
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

    # Persist in SQLite
    save_commitment(commitment)
    _sync_commitment_from_chain(commitment)

    # Record tamper-evident audit event
    record_audit_event(
        action="PROPOSAL_AUTHORIZED",
        actor=actor.id,
        role=actor.role,
        commitment_id=commitment_id,
        details={
            "proposal_id": proposal_id,
            "approved_by": approval.approved_by,
            "onchain_reservation_id": commitment.get("onchain_reservation_id"),
            "tx_hash": commitment.get("tx_hash"),
            "block_number": commitment.get("block_number"),
        },
    )

    return {"proposal_id": proposal_id, "status": "authorized", "commitment": commitment}


@app.get("/commitments")
def list_commitments():
    # Sync each persistent commitment with latest on-chain state
    commitments = db_list_commitments()
    for commitment in commitments:
        _sync_commitment_from_chain(commitment)
    return commitments


@app.post("/commitments/{commitment_id}/accept")
def accept_commitment(
    commitment_id: str,
    x_actor_id: Optional[str] = Header(None, alias="X-Actor-Id"),
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
):
    commitment = get_commitment(commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")

    provider_id = commitment.get("provider_id", "lab-c")

    # RBAC Guard: verify caller is the designated provider with valid credentials
    actor = authenticate_actor(
        x_actor_id=x_actor_id,
        x_api_key=x_api_key,
        default_actor_id=provider_id,
    )
    verify_acceptance_permission(actor, provider_id)

    reservation_id = commitment.get("onchain_reservation_id")
    if not reservation_id:
        raise HTTPException(
            status_code=400,
            detail="Commitment does not have an on-chain reservation ID to accept.",
        )

    accept_res = accept_onchain_reservation(reservation_id, provider=provider_id)
    if not accept_res.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"Coral acceptance failed: {accept_res.get('error')}",
        )

    commitment["status"] = "committed"
    commitment["onchain_acceptance_tx_hash"] = accept_res["txHash"]
    commitment["acceptance_block"] = accept_res["blockNumber"]
    save_commitment(commitment)
    _sync_commitment_from_chain(commitment)

    # Record audit event
    record_audit_event(
        action="COMMITMENT_ACCEPTED",
        actor=actor.id,
        role=actor.role,
        commitment_id=commitment_id,
        details={
            "reservation_id": reservation_id,
            "tx_hash": accept_res["txHash"],
            "block_number": accept_res["blockNumber"],
        },
    )

    return {
        "status": "committed",
        "commitment": commitment,
        "blockchain": accept_res,
    }


@app.get("/commitments/{commitment_id}/onchain")
def get_commitment_onchain(commitment_id: str):
    commitment = get_commitment(commitment_id)
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
def fulfill_commitment(
    commitment_id: str,
    x_actor_id: Optional[str] = Header(None, alias="X-Actor-Id"),
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
):
    commitment = get_commitment(commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")

    provider_id = commitment.get("provider_id", "lab-c")

    # RBAC Guard: verify caller is the designated provider with valid credentials
    actor = authenticate_actor(
        x_actor_id=x_actor_id,
        x_api_key=x_api_key,
        default_actor_id=provider_id,
    )
    verify_fulfillment_permission(actor, provider_id)

    _sync_commitment_from_chain(commitment)
    current_status = commitment.get("status")
    onchain_status = commitment.get("onchain_status")

    if current_status == "fulfilled":
        raise HTTPException(status_code=409, detail="Commitment is already fulfilled")

    if onchain_status and onchain_status != "Committed":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot fulfill reservation with on-chain status '{onchain_status}'. Reservation must be 'Committed' by provider first.",
        )

    reservation_id = commitment.get("onchain_reservation_id")
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
        save_commitment(commitment)
        _sync_commitment_from_chain(commitment)

        record_audit_event(
            action="COMMITMENT_FULFILLED",
            actor=actor.id,
            role=actor.role,
            commitment_id=commitment_id,
            details={
                "reservation_id": reservation_id,
                "tx_hash": fulfill_res["txHash"],
                "block_number": fulfill_res["blockNumber"],
                "relayer_topup_tx": fulfill_res.get("relayerTopUpTx"),
            },
        )

        return {
            "status": "fulfilled",
            "commitment": commitment,
            "blockchain": fulfill_res,
        }
    else:
        commitment["status"] = "fulfilled"
        commitment["note"] = "Fulfilled in off-chain demo ledger."
        save_commitment(commitment)
        return {
            "status": "fulfilled",
            "commitment": commitment,
        }


@app.post("/commitments/{commitment_id}/dispute")
def dispute_commitment(
    commitment_id: str,
    dispute: DisputeRequest,
    x_actor_id: Optional[str] = Header(None, alias="X-Actor-Id"),
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
):
    commitment = get_commitment(commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")

    requester_id = commitment.get("requesting_institution_id", "hospital-a")
    provider_id = commitment.get("provider_id", "lab-c")

    # RBAC Guard: actor must be requester or provider with valid credentials
    actor = authenticate_actor(
        x_actor_id=x_actor_id,
        x_api_key=x_api_key,
        default_actor_id=dispute.disputed_by,
    )
    verify_dispute_permission(actor, requester_id, provider_id)

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

    # Evidence Integrity: Store raw document and compute cryptographic SHA-256 digest
    raw_document = dispute.reason
    stored_evidence = store_evidence(
        commitment_id=commitment_id,
        evidence_type="dispute_claim",
        raw_document=raw_document,
        submitted_by=actor.id,
    )
    computed_evidence_hash = stored_evidence["computed_sha256"]

    dispute_res = dispute_onchain_reservation(
        reservation_id=reservation_id,
        actor=actor.id,
        reason=dispute.reason,
        evidence_hash=computed_evidence_hash,
    )

    if not dispute_res.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"On-chain dispute initiation failed: {dispute_res.get('error')}",
        )

    commitment["dispute_tx_hash"] = dispute_res["txHash"]
    commitment["dispute_block"] = dispute_res["blockNumber"]
    commitment["dispute_reason"] = dispute.reason
    commitment["dispute_evidence"] = dispute_res["evidenceHash"]
    save_commitment(commitment)
    _sync_commitment_from_chain(commitment)

    record_audit_event(
        action="DISPUTE_RAISED",
        actor=actor.id,
        role=actor.role,
        commitment_id=commitment_id,
        details={
            "reservation_id": reservation_id,
            "reason": dispute.reason,
            "evidence_hash": dispute_res["evidenceHash"],
            "tx_hash": dispute_res["txHash"],
            "block_number": dispute_res["blockNumber"],
        },
    )

    return {
        "status": "disputed",
        "commitment": commitment,
        "evidence_record": stored_evidence,
        "blockchain": dispute_res,
    }


@app.post("/commitments/{commitment_id}/resolve")
def resolve_dispute(
    commitment_id: str,
    resolution: ResolveDisputeRequest,
    x_actor_id: Optional[str] = Header(None, alias="X-Actor-Id"),
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
):
    commitment = get_commitment(commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")

    # Critical Safeguard: Verify caller has role 'arbitrator' or 'admin' and valid credentials
    actor = authenticate_actor(
        x_actor_id=x_actor_id,
        x_api_key=x_api_key,
        default_actor_id="consortium-arbitrator",
    )
    verify_arbitration_permission(actor)

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

    # Evidence Integrity: Store formal arbitration findings and compute SHA-256
    raw_findings = f"RESOLUTION: {resolution.resolution}\nFINDINGS: {resolution.notes}"
    stored_resolution = store_evidence(
        commitment_id=commitment_id,
        evidence_type="arbitration_ruling",
        raw_document=raw_findings,
        submitted_by=actor.id,
    )
    computed_resolution_evidence = stored_resolution["computed_sha256"]

    resolve_res = resolve_onchain_dispute(
        reservation_id=reservation_id,
        resolution=resolution.resolution,
        notes=resolution.notes,
        resolution_evidence=computed_resolution_evidence,
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
    save_commitment(commitment)
    _sync_commitment_from_chain(commitment)

    record_audit_event(
        action="DISPUTE_RESOLVED",
        actor=actor.id,
        role=actor.role,
        commitment_id=commitment_id,
        details={
            "reservation_id": reservation_id,
            "resolution": resolution.resolution,
            "notes": resolution.notes,
            "resolution_evidence": resolve_res["resolutionEvidence"],
            "tx_hash": resolve_res["txHash"],
            "block_number": resolve_res["blockNumber"],
        },
    )

    return {
        "status": "resolved",
        "commitment": commitment,
        "resolution_record": stored_resolution,
        "blockchain": resolve_res,
    }


@app.get("/commitments/{commitment_id}/evidence")
def get_commitment_evidence(commitment_id: str):
    """Retrieve all raw off-chain evidence documents and independently recompute their SHA-256 hashes."""
    commitment = get_commitment(commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")

    records = get_evidence_records(commitment_id=commitment_id)
    return {
        "commitment_id": commitment_id,
        "total_evidence_records": len(records),
        "evidence": records,
    }


@app.get("/commitments/{commitment_id}/audit")
def get_commitment_audit_trail(commitment_id: str):
    """Retrieve the tamper-evident, hash-chained audit log for a specific commitment."""
    commitment = get_commitment(commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")

    events = get_audit_trail(commitment_id=commitment_id)
    return {
        "commitment_id": commitment_id,
        "total_audit_events": len(events),
        "audit_trail": events,
    }


@app.get("/audit/verify")
def verify_audit_log_integrity():
    """Verify cryptographic hash-chain continuity from genesis to latest event."""
    return verify_audit_trail_integrity()


@app.get("/audit")
def get_all_audit_logs(limit: int = 50):
    """Retrieve global system audit events with cryptographic hash chains."""
    return get_audit_trail(limit=limit)
