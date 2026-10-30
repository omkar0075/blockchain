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
from app.blockchain import get_blockchain_status, get_onchain_reservation
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
        print(f"  [+] Successfully recovered commitment from fresh DB connection.")
        print(f"  [+] Status: {row['status']} | Provider: {row['provider_id']} | Block: #{row['block_number']}")
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
        x_api_key="medswarm-key-hosp-a-sec91",
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

    print("  RESULT: PASSED (All RBAC permission guards verified)")


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


def test_coral_live_state():
    print_header("TEST 6: Coral Testnet Live Reservation State Confirmation")
    live_id = "0xa550bd19b71e87ff0515504c9bf6d2d03e8a428174772650f8305acab0ac7ccf"
    onchain = get_onchain_reservation(live_id)
    assert onchain.get("success"), "Failed to query Coral Testnet!"
    assert onchain.get("exists"), f"Reservation {live_id} not found on Coral!"

    res = onchain["reservation"]
    print(f"  [+] Live Reservation ID:  {live_id}")
    print(f"  [+] Requester (Hospital): {res['requester']}")
    print(f"  [+] Provider (Lab):       {res['provider']}")
    print(f"  [+] Status Code:          {res['statusCode']} ({res['status']})")
    print(f"  [+] Fulfilled At:         {res['fulfilledAt']}")
    print(f"  [+] Disputed By:          {res['disputedBy']}")
    print(f"  [+] Dispute Evidence:     {res['disputeEvidence']}")
    print("  RESULT: PASSED (Confirmed directly against Coral Testnet RPC)")


def main():
    print("\n" + "=" * 65)
    print(" MEDSWARM COMPREHENSIVE SYSTEM VERIFICATION SUITE")
    print("=" * 65)

    try:
        test_sqlite_persistence()
        test_authentication_and_rbac()
        test_arbitration_alignment()
        test_evidence_integrity()
        test_audit_hash_chain_tamper_detection()
        test_coral_live_state()

        print("\n" + "=" * 65)
        print(" ALL 6 VERIFICATION TEST MODULES PASSED SUCCESSFULLY!")
        print("=" * 65 + "\n")
    except Exception as e:
        print(f"\n[FAILED] System verification failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

