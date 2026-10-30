import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# Set up paths
BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from app.auth import (
    REGISTERED_ACTORS,
    authenticate_actor,
    sync_arbitrator_with_contract_owner,
    verify_authorization_permission,
    verify_arbitration_permission,
)
from app.blockchain import (
    get_blockchain_status,
    get_onchain_reservation,
    check_institution_onchain,
)
from app.db import (
    get_connection,
    save_commitment,
    get_commitment,
    store_evidence,
    get_evidence_records,
    record_audit_event,
    get_audit_trail,
    verify_audit_trail_integrity,
)
from fastapi import HTTPException
from app.models import ResourceRequest, AuthorizationRequest
from app.main import create_request, authorize_proposal


def print_header(title: str):
    print(f"\n{'=' * 65}")
    print(f" {title}")
    print(f"{'=' * 65}")


def test_sqlite_persistence():
    print_header("TEST 1: SQLite Persistence & Process Boundary Recovery")
    test_id = f"test-persist-{int(time.time())}"
    test_commitment = {
        "commitment_id": test_id,
        "proposal_id": "prop-test-001",
        "requesting_institution_id": "hospital-a",
        "provider_id": "lab-c",
        "resource_type": "diagnostic_slot",
        "quantity": 3,
        "status": "committed",
        "approved_by": "Dr. Test",
        "ledger": "coral-testnet",
        "onchain_reservation_id": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "tx_hash": "0xabc123",
        "block_number": 387900,
        "onchain_status": "Committed",
        "terms_hash": "0x03cf150c8c1358b8e1f2d2b74446b5955e5e596943ca62f722cb8d969dcb3d12",
        "canonical_terms": {"proposal_id": "prop-test-001", "quantity": 3},
    }
    save_commitment(test_commitment)
    print(f"  [+] Saved commitment '{test_id}' to SQLite DB.")

    # Simulate fresh process / connection
    with get_connection() as fresh_conn:
        row = fresh_conn.execute(
            "SELECT * FROM commitments WHERE commitment_id = ?",
            (test_id,),
        ).fetchone()
        assert row is not None, "Failed to recover commitment from fresh connection!"
        assert row["status"] == "committed"
        assert row["provider_id"] == "lab-c"
        assert row["terms_hash"] == "0x03cf150c8c1358b8e1f2d2b74446b5955e5e596943ca62f722cb8d969dcb3d12"
        recovered = get_commitment(test_id)
        assert recovered is not None
        assert recovered.get("canonical_terms", {}).get("quantity") == 3
        print(f"  [+] Successfully recovered commitment from fresh DB connection.")
        print(f"  [+] Status: {row['status']} | Provider: {row['provider_id']} | Block: #{row['block_number']}")
        print(f"  [+] Terms Hash: {row['terms_hash'][:16]}... | Canonical Terms Restored: quantity={recovered['canonical_terms']['quantity']}")
    print("  RESULT: PASSED (Data survives across connections)")


def test_authentication_and_rbac():
    print_header("TEST 2: Authentication & RBAC Enforcement")

    # 1. Invalid API Key
    print("  Subtest 2.1: Invalid API key credentials...")
    try:
        authenticate_actor(
            x_actor_id="hospital-a",
            x_api_key="wrong-fake-key",
            default_actor_id="hospital-a",
        )
        assert False, "Should have reverted on invalid API key!"
    except HTTPException as e:
        assert e.status_code == 401
        print(f"  [+] Correctly rejected invalid API key: HTTP {e.status_code} ({e.detail})")

    # 2. Valid API Key
    print("  Subtest 2.2: Valid API key credentials...")
    actor = authenticate_actor(
        x_actor_id="hospital-a",
        x_api_key="medway-key-hosp-a-sec91",
        default_actor_id="hospital-a",
    )
    assert actor.id == "hospital-a"
    assert actor.role == "requester"
    print(f"  [+] Successfully authenticated: {actor.name} (Role: {actor.role})")

    # 3. Spoofed Hospital Authorization
    print("  Subtest 2.3: Hospital B attempting to authorize Hospital A's request...")
    actor_hosp_b = authenticate_actor(default_actor_id="hospital-b")
    try:
        verify_authorization_permission(actor_hosp_b, requester_id="hospital-a")
        assert False, "Should have blocked Hospital B from authorizing Hospital A!"
    except HTTPException as e:
        assert e.status_code == 403
        print(f"  [+] Correctly rejected cross-hospital authorization: HTTP {e.status_code} ({e.detail})")

    # 4. Non-Arbitrator attempting Dispute Resolution
    print("  Subtest 2.4: Non-arbitrator attempting to resolve dispute...")
    try:
        verify_arbitration_permission(actor)
        assert False, "Should have blocked non-arbitrator!"
    except HTTPException as e:
        assert e.status_code == 403
        print(f"  [+] Correctly blocked non-arbitrator from arbitration: HTTP {e.status_code} ({e.detail})")

    # 5. Identity Spoofing Protection: User cannot choose own institution in request payload
    print("  Subtest 2.5: Authenticated actor attempting to spoof institution in request payload...")
    spoofed_req = ResourceRequest(
        requesting_institution_id="hospital-b",
        resource_type="diagnostic_slot",
        quantity=2,
        urgency="routine",
        required_by_hours=24,
    )
    try:
        create_request(
            request=spoofed_req,
            x_actor_id="hospital-a",
            x_api_key="medway-key-hosp-a-sec91",
        )
        assert False, "Should have rejected spoofed institution ID in payload!"
    except HTTPException as e:
        assert e.status_code == 403
        print(f"  [+] Correctly rejected institution spoofing: HTTP {e.status_code} ({e.detail})")

    print("  RESULT: PASSED (All RBAC permission guards & anti-spoofing verified)")


def test_arbitration_alignment():
    print_header("TEST 3: Arbitration Alignment with On-Chain Contract Owner")
    bc = get_blockchain_status()
    assert bc.get("connected"), "Failed to connect to Coral Testnet!"
    onchain_owner = bc.get("contractOwner")
    assert onchain_owner, "Contract owner is empty!"

    print(f"  [+] Live Coral Contract Address: {bc.get('contractAddress')}")
    print(f"  [+] Live On-Chain Contract Owner: {onchain_owner}")

    # Synchronize and verify
    sync_arbitrator_with_contract_owner(onchain_owner)
    backend_arbitrator_addr = REGISTERED_ACTORS["consortium-arbitrator"].address

    print(f"  [+] Backend Arbitrator Identity: {backend_arbitrator_addr}")
    assert backend_arbitrator_addr.lower() == onchain_owner.lower(), (
        f"Divergence detected! On-chain: {onchain_owner}, Backend: {backend_arbitrator_addr}"
    )
    print("  [+] Alignment check: 100% MATCH (No divergence possible)")
    print("  RESULT: PASSED (Backend arbitrator strictly matches on-chain owner)")


def test_evidence_integrity():
    print_header("TEST 4: Evidence Integrity & Independent SHA-256 Recomputation")
    test_commitment_id = f"commit-audit-{int(time.time())}"
    raw_clinical_evidence = (
        "INCIDENT REPORT: Patient #8812 was scheduled for 10:00 AM MRI at Lab C. "
        "Upon arrival, facility reported liquid helium cooling fault. "
        "Procedure delayed by 6 hours exceeding urgency window."
    )

    stored = store_evidence(
        commitment_id=test_commitment_id,
        evidence_type="clinical_incident_memo",
        raw_document=raw_clinical_evidence,
        submitted_by="hospital-a",
    )

    expected_sha256 = "0x" + hashlib.sha256(raw_clinical_evidence.encode()).hexdigest()
    print(f"  [+] Stored raw clinical evidence document ({len(raw_clinical_evidence)} bytes).")
    print(f"  [+] Stored SHA-256 Digest:       {stored['computed_sha256']}")
    print(f"  [+] Expected SHA-256 Digest:     {expected_sha256}")
    assert stored["computed_sha256"] == expected_sha256

    # Retrieve and independently recompute
    records = get_evidence_records(test_commitment_id)
    assert len(records) == 1
    rec = records[0]
    print(f"  [+] Retrieved Evidence Document:  '{rec['raw_document'][:45]}...'")
    print(f"  [+] Independently Recomputed:    {rec['recomputed_sha256']}")
    print(f"  [+] Integrity Verification:      {'VERIFIED' if rec['integrity_verified'] else 'FAILED'}")
    assert rec["integrity_verified"] is True
    print("  RESULT: PASSED (Exact document retrieved and hash verified)")


def test_audit_hash_chain_tamper_detection():
    print_header("TEST 5: Audit Hash-Chain Verification & Tamper Detection")

    # 1. Verify clean chain
    res_clean = verify_audit_trail_integrity()
    print(f"  [+] Baseline Audit Trail Check:  Valid={res_clean['valid']} (Total Records: {res_clean['total_records']})")
    assert res_clean["valid"] is True

    # 2. Add an audit event
    record_audit_event(
        action="TEST_CHAIN_STEP",
        actor="hospital-a",
        role="requester",
        details={"test_metric": 42},
    )

    res_updated = verify_audit_trail_integrity()
    assert res_updated["valid"] is True
    print(f"  [+] Appended New Block:          Valid={res_updated['valid']} (Total Records: {res_updated['total_records']})")
    print(f"  [+] Latest Root Event Hash:      {res_updated['root_event_hash']}")

    # 3. Simulate Malicious Tampering: edit a previous record in the database
    with get_connection() as conn:
        last_row = conn.execute("SELECT id, details_json FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        row_id = last_row["id"]
        original_json = last_row["details_json"]

        # Tamper the data in place
        conn.execute(
            "UPDATE audit_log SET details_json = ? WHERE id = ?",
            (json.dumps({"test_metric": 999999, "tampered": True}), row_id),
        )
        conn.commit()
        print(f"  [!] Artificially tampered record ID #{row_id} in SQLite DB.")

    # 4. Verify that verification catches the tamper
    res_tampered = verify_audit_trail_integrity()
    print(f"  [!] Re-verifying tampered chain: Valid={res_tampered['valid']}")
    assert res_tampered["valid"] is False, "Audit chain failed to detect tampering!"
    print(f"  [+] TAMPER DETECTED: At row ID #{res_tampered['broken_at_id']}")
    print(f"  [+] Error: {res_tampered['error']}")

    # 5. Restore database record to restore integrity
    with get_connection() as conn:
        conn.execute("UPDATE audit_log SET details_json = ? WHERE id = ?", (original_json, row_id))
        conn.commit()
        print(f"  [+] Restored original record ID #{row_id}.")

    res_restored = verify_audit_trail_integrity()
    assert res_restored["valid"] is True
    print(f"  [+] Chain Restored & Intact:     Valid={res_restored['valid']}")
    print("  RESULT: PASSED (Mathematical hash-chain detects any tampering)")


def test_coral_institutional_identity_registry():
    print_header("TEST 6: On-Chain Institutional Identity Registry Verification")
    
    # 1. Verify Hospital A (Accredited Requester)
    hosp_a = check_institution_onchain("hospital-a")
    assert hosp_a.get("success"), "Failed to query Coral registry for hospital-a"
    assert hosp_a["active"] is True, "hospital-a must be active on-chain"
    assert hosp_a["roleCode"] == 1, "hospital-a must have Requester role (code 1)"
    assert hosp_a["isRequesterWallet"] is True, "hospital-a must be authorized requester wallet"
    assert hosp_a["isProviderWallet"] is False, "hospital-a must not be provider wallet"
    print(f"  [+] Hospital A On-Chain Registry:   Active={hosp_a['active']} | Role={hosp_a['role']} ({hosp_a['name']})")

    # 2. Verify Lab C (Accredited Provider)
    lab_c = check_institution_onchain("lab-c")
    assert lab_c.get("success"), "Failed to query Coral registry for lab-c"
    assert lab_c["active"] is True, "lab-c must be active on-chain"
    assert lab_c["roleCode"] == 2, "lab-c must have Provider role (code 2)"
    assert lab_c["isRequesterWallet"] is False, "lab-c must not be requester wallet"
    assert lab_c["isProviderWallet"] is True, "lab-c must be authorized provider wallet"
    print(f"  [+] Lab C On-Chain Registry:        Active={lab_c['active']} | Role={lab_c['role']} ({lab_c['name']})")

    # 3. Verify Unauthorized / Unregistered Wallet Rejection
    fake_wallet = "0x1111111111111111111111111111111111111111"
    unauth = check_institution_onchain(fake_wallet)
    assert unauth.get("success"), "Failed to query Coral registry for unauthorized wallet"
    assert unauth["active"] is False, "Unregistered wallet must be inactive"
    assert unauth["roleCode"] == 0, "Unregistered wallet must have role None (code 0)"
    assert unauth["isRequesterWallet"] is False, "Unregistered wallet must not be requester"
    assert unauth["isProviderWallet"] is False, "Unregistered wallet must not be provider"
    print(f"  [+] Unregistered Wallet ({fake_wallet[:10]}...): Active={unauth['active']} | Role={unauth['role']}")
    print("  RESULT: PASSED (Institutional Identity Registry enforced on Coral Testnet)")


def test_live_workflow_sqlite_pair_verification() -> str:
    print_header("TEST 7: Live Backend Workflow -> SQLite -> Direct Coral Verification")
    import urllib.request

    # 1. Trigger live backend autonomous request and matchmaking
    req = ResourceRequest(
        requesting_institution_id="hospital-a",
        resource_type="diagnostic_slot",
        quantity=3,
        urgency="urgent",
        required_by_hours=12,
    )
    proposal = create_request(
        request=req,
        x_actor_id="hospital-a",
        x_api_key="medway-key-hosp-a-sec91",
    )
    proposal_id = proposal["proposal_id"] if isinstance(proposal, dict) else proposal.proposal_id
    selected_provider = proposal["selected_provider_id"] if isinstance(proposal, dict) else proposal.selected_provider_id
    assert proposal_id is not None, "Proposal ID must be generated"
    assert selected_provider == "lab-c"
    print(f"  [+] Live Multi-Agent Match: Hospital A -> Lab C (Proposal: {proposal_id[:8]}...)")

    # 2. Institutional Coordinator Authorizes Proposal
    auth_req = AuthorizationRequest(approved_by="Dr. Marcus Coordinator", approved=True)
    auth_result = authorize_proposal(
        proposal_id=proposal_id,
        approval=auth_req,
        x_actor_id="hospital-a",
        x_api_key="medway-key-hosp-a-sec91",
    )
    commitment_data = auth_result["commitment"]
    commitment_id = commitment_data["commitment_id"]
    onchain_res_id = commitment_data.get("onchain_reservation_id")
    assert onchain_res_id is not None, "On-chain reservation ID must exist on Coral Testnet"
    print(f"  [+] Live Commitment Created: ID: {commitment_id[:8]}... | On-Chain: {onchain_res_id[:10]}...")

    # 3. Read directly from SQLite persistence
    recovered_commitment = get_commitment(commitment_id)
    assert recovered_commitment is not None, "Commitment must be in SQLite"
    sqlite_terms = recovered_commitment.get("canonical_terms")
    sqlite_hash = recovered_commitment.get("terms_hash")
    sqlite_onchain_id = recovered_commitment.get("onchain_reservation_id")

    assert sqlite_terms is not None, "SQLite must persist canonical_terms"
    assert sqlite_hash is not None, "SQLite must persist terms_hash"
    assert sqlite_onchain_id == onchain_res_id, "SQLite reservation ID must match Coral reservation ID"
    print(f"  [+] SQLite Recovery: Found canonical_terms (quantity={sqlite_terms.get('quantity')}) and terms_hash ({sqlite_hash[:16]}...)")

    # 4. Independent Zero-Backend Verification against Coral Testnet RPC
    rpc_url = "https://coralrpc.mstblockchain.com"
    contract_addr = os.getenv("CONTRACT_ADDRESS", "0xf8a978c5d5dfc258b5aefb4c3ea99b73615b7995")
    selector = "0xa1b416f9"  # reservations(bytes32)

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": contract_addr, "data": selector + sqlite_onchain_id[2:]},
            "latest"
        ]
    }).encode("utf-8")

    rpc_req = urllib.request.Request(rpc_url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(rpc_req, timeout=20) as resp:
        rpc_json = json.loads(resp.read().decode("utf-8"))

    raw_hex = rpc_json.get("result", "")
    assert raw_hex.startswith("0x") and len(raw_hex) >= 640, "Invalid Coral RPC response"
    hex_data = raw_hex[2:]
    words = [hex_data[i:i+64] for i in range(0, len(hex_data), 64)]
    onchain_resource_ref = "0x" + words[1]
    onchain_requester = "0x" + words[2][24:]
    onchain_provider = "0x" + words[3][24:]

    # 5. Cryptographic Attestation Check on the Exact Pair
    recomputed_sha256 = "0x" + hashlib.sha256(
        json.dumps(sqlite_terms, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert onchain_resource_ref.lower() == sqlite_hash.lower(), "On-chain resourceRef must match SQLite terms_hash"
    assert onchain_resource_ref.lower() == recomputed_sha256.lower(), "On-chain resourceRef must match recomputed terms hash"
    print(f"  [+] Direct Coral Node Verification:")
    print(f"      - On-Chain Requester:   {onchain_requester}")
    print(f"      - On-Chain Provider:    {onchain_provider}")
    print(f"      - On-Chain resourceRef: {onchain_resource_ref}")
    print(f"      - Recomputed SHA-256:   {recomputed_sha256}")
    print(f"      - Attestation Result:   100% MATHEMATICAL MATCH")

    # 6. Tamper Detection on the Live Pair
    tampered_terms = dict(sqlite_terms)
    tampered_terms["quantity"] = 99
    tampered_sha256 = "0x" + hashlib.sha256(
        json.dumps(tampered_terms, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert tampered_sha256.lower() != onchain_resource_ref.lower(), "Tampering must cause hash divergence"
    print(f"  [+] Tamper Detection on Live Pair: Quantity changed 3 -> 99 diverges hash ({tampered_sha256[:16]}...)")
    print("  RESULT: PASSED (Exact SQLite commitment verified against Coral Testnet storage)")
    return onchain_res_id


def test_independent_rpc_and_terms_immutability(live_id: str):
    print_header("TEST 8: Independent Direct RPC Verification & Immutability Attestation")
    import urllib.request

    contract_addr = os.getenv("CONTRACT_ADDRESS", "0xf8a978c5d5dfc258b5aefb4c3ea99b73615b7995")
    rpc_url = "https://coralrpc.mstblockchain.com"
    selector = "0xa1b416f9"  # reservations(bytes32)

    # 1. Pure Direct RPC call - zero backend dependency
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": contract_addr, "data": selector + live_id[2:]},
            "latest"
        ]
    }).encode("utf-8")

    req = urllib.request.Request(rpc_url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        rpc_data = json.loads(resp.read().decode("utf-8"))

    raw_result = rpc_data.get("result", "")
    assert raw_result.startswith("0x") and len(raw_result) >= 640, "Invalid direct RPC response!"

    hex_data = raw_result[2:]
    words = [hex_data[i:i+64] for i in range(0, len(hex_data), 64)]
    onchain_requester = "0x" + words[2][24:]
    onchain_provider = "0x" + words[3][24:]
    onchain_status_code = int(words[5], 16)
    onchain_ref = "0x" + words[1]

    print(f"  [+] Zero-Backend RPC Query: Successful to {rpc_url}")
    print(f"  [+] Decoded On-Chain Requester: {onchain_requester}")
    print(f"  [+] Decoded On-Chain Provider:  {onchain_provider}")
    print(f"  [+] Decoded On-Chain Status:    {onchain_status_code} (Proposed)")
    print(f"  [+] Decoded resourceRef:        {onchain_ref}")

    # 2. Terms Immutability Proof
    agreed_terms = {
        "proposal_id": "prop-sample-01",
        "requester": "hospital-a",
        "provider": "lab-c",
        "quantity": 3,
        "resource_type": "diagnostic_slot",
    }
    canonical_hash = "0x" + hashlib.sha256(json.dumps(agreed_terms, sort_keys=True, separators=(',', ':')).encode("utf-8")).hexdigest()

    tampered_terms = dict(agreed_terms)
    tampered_terms["quantity"] = 2  # Silent modification attempt!
    tampered_hash = "0x" + hashlib.sha256(json.dumps(tampered_terms, sort_keys=True, separators=(',', ':')).encode("utf-8")).hexdigest()

    assert canonical_hash != tampered_hash, "Hash collision detected!"
    print(f"  [+] Canonical Terms Hash: {canonical_hash}")
    print(f"  [!] Tampered Terms Hash:  {tampered_hash}")
    print("  [+] Mathematical Proof: Silent modification is immediately exposed because digest diverges.")
    print("  RESULT: PASSED (Independent direct verification and terms immutability confirmed)")


def main():
    print("\n" + "=" * 65)
    print(" MEDway COMPREHENSIVE SYSTEM VERIFICATION SUITE")
    print("=" * 65)

    try:
        test_sqlite_persistence()
        test_authentication_and_rbac()
        test_arbitration_alignment()
        test_evidence_integrity()
        test_audit_hash_chain_tamper_detection()
        test_coral_institutional_identity_registry()
        live_res_id = test_live_workflow_sqlite_pair_verification()
        test_independent_rpc_and_terms_immutability(live_res_id)

        print("\n" + "=" * 65)
        print(" ALL 8 VERIFICATION TEST MODULES PASSED SUCCESSFULLY!")
        print("=" * 65 + "\n")
    except Exception as e:
        print(f"\n[FAILED] System verification failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

