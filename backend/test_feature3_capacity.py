import sys
import time
from pathlib import Path
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from app.main import app

def run_tests():
    client = TestClient(app)
    print("=" * 75)
    print(" FEATURE 3: AUTONOMOUS CAPACITY & TWO-PHASE COMMIT VERIFICATION")
    print("=" * 75)

    headers_hosp_a = {
        "X-Actor-Id": "hospital-a",
        "X-Api-Key": "medway-key-hosp-a-sec91",
    }
    headers_lab_c = {
        "X-Actor-Id": "lab-c",
        "X-Api-Key": "medway-key-lab-c-sec73",
    }

    # 1. Inspect on-chain capacity for Lab C
    print("\n[1] Querying real-time on-chain capacity for Lab C via GET /capacity/lab-c/diagnostic_slot...")
    res = client.get("/capacity/lab-c/diagnostic_slot")
    assert res.status_code == 200, f"Query capacity failed: {res.text}"
    cap = res.json()
    print(f"    [+] Total: {cap.get('totalCapacity')}, Available: {cap.get('availableCapacity')}, Locked: {cap.get('lockedCapacity')}, Committed: {cap.get('committedCapacity')}")
    assert cap.get("success") is True
    assert cap.get("availableCapacity") >= 1, "Expected at least 1 available slot"

    # 2. Submit resource request (AI Agent Discovery + Phase 1 Temporary On-Chain Lock)
    print("\n[2] Submitting resource request for 2 diagnostic slots from Hospital A...")
    res = client.post(
        "/requests",
        headers=headers_hosp_a,
        json={
            "requesting_institution_id": "hospital-a",
            "resource_type": "diagnostic_slot",
            "quantity": 2,
            "urgency": "urgent",
            "required_by_hours": 24,
        },
    )
    assert res.status_code == 200, f"Request failed: {res.text}"
    prop = res.json()
    proposal_id = prop["proposal_id"]
    lock_id = prop.get("lock_id")
    print(f"    [+] Proposal Created: {proposal_id}")
    print(f"    [+] Selected Provider: {prop.get('selected_provider_id')}")
    print(f"    [+] Temporary On-Chain Lock ID: {lock_id}")
    print(f"    [+] Lock Status: {prop.get('lock_status')}")
    print(f"    [+] Lock Tx Hash: {prop.get('lock_tx_hash')}")
    assert lock_id is not None, "Expected an active on-chain capacity lock"
    assert prop.get("lock_status") == "Active"

    # Verify that AI agent marked on-chain verification
    offers = prop.get("offers", [])
    lab_c_offer = next((o for o in offers if o["provider_id"] == "lab-c"), None)
    assert lab_c_offer is not None
    assert lab_c_offer.get("onchain_verified") is True
    print(f"    [+] AI Agent Offer Rationale: {lab_c_offer.get('rationale')}")

    # 3. Query capacity again to verify on-chain lock hold
    res = client.get("/capacity/lab-c/diagnostic_slot")
    cap_after_lock = res.json()
    print(f"    [+] Capacity After Lock: Available={cap_after_lock.get('availableCapacity')}, Locked={cap_after_lock.get('lockedCapacity')}")
    assert cap_after_lock.get("lockedCapacity") >= 2

    # 4. Verify Inspect Lock Endpoint
    print(f"\n[3] Inspecting Lock details via GET /locks/{lock_id}...")
    res = client.get(f"/locks/{lock_id}")
    assert res.status_code == 200, f"Inspect lock failed: {res.text}"
    lock_info = res.json()
    assert lock_info.get("exists") is True
    assert lock_info["lock"]["status"] == "Active"
    print(f"    [+] On-Chain Lock Confirmed: Quantity={lock_info['lock']['quantity']}, ExpiresAt={lock_info['lock']['expiresAt']}")

    # 5. Authorize Proposal (Phase 2 Commit: Atomic Lock-to-Reservation Conversion)
    print("\n[4] Hospital A Coordinator Authorizing Proposal (Atomic Lock Conversion)...")
    res = client.post(
        f"/proposals/{proposal_id}/authorize",
        headers=headers_hosp_a,
        json={"approved": True, "approved_by": "Dr. Sarah Chen, Chief Medical Officer"},
    )
    assert res.status_code == 200, f"Authorization failed: {res.text}"
    auth_data = res.json()
    commitment = auth_data["commitment"]
    commitment_id = commitment["commitment_id"]
    res_id = commitment.get("onchain_reservation_id")
    print(f"    [+] Commitment ID: {commitment_id}")
    print(f"    [+] On-Chain Reservation ID: {res_id}")
    print(f"    [+] Transaction Hash: {commitment.get('tx_hash')}")
    print(f"    [+] Lock ID bound: {commitment.get('lock_id')}")
    assert res_id is not None, "Expected on-chain reservation ID"
    assert commitment.get("lock_id") == lock_id

    # 6. Verify capacity shifted from Locked to Committed
    res = client.get("/capacity/lab-c/diagnostic_slot")
    cap_after_conv = res.json()
    print(f"    [+] Capacity After Conversion: Locked={cap_after_conv.get('lockedCapacity')}, Committed={cap_after_conv.get('committedCapacity')}")
    assert cap_after_conv.get("committedCapacity") >= 2

    # 7. Provider Accepts Reservation
    print("\n[5] Lab C Accepting Reservation via POST /commitments/{id}/accept...")
    res = client.post(
        f"/commitments/{commitment_id}/accept",
        headers=headers_lab_c,
    )
    assert res.status_code == 200, f"Accept failed: {res.text}"
    accept_data = res.json()
    print(f"    [+] Status: {accept_data.get('status')}, On-Chain: {accept_data.get('onchain_status')}")

    # 8. Provider Fulfills Reservation (Releases Committed Capacity On-Chain)
    print("\n[6] Lab C Fulfilling Reservation via POST /commitments/{id}/fulfill...")
    res = client.post(
        f"/commitments/{commitment_id}/fulfill",
        headers=headers_lab_c,
    )
    assert res.status_code == 200, f"Fulfill failed: {res.text}"
    fulfill_data = res.json()
    print(f"    [+] Status: {fulfill_data.get('status')}, FulfilledAt: {fulfill_data.get('fulfilled_at')}")

    # 9. Verify committed capacity was automatically released back to available
    res = client.get("/capacity/lab-c/diagnostic_slot")
    cap_after_fulfill = res.json()
    print(f"    [+] Capacity After Fulfillment: Available={cap_after_fulfill.get('availableCapacity')}, Committed={cap_after_fulfill.get('committedCapacity')}")
    assert cap_after_fulfill.get("committedCapacity") == cap_after_conv.get("committedCapacity") - 2
    assert cap_after_fulfill.get("availableCapacity") == cap_after_conv.get("availableCapacity") + 2

    # 10. Test Proposal Decline Releases Lock back to available
    print("\n[7] Testing Coordinator Decline Releases Capacity Lock...")
    res = client.post(
        "/requests",
        headers=headers_hosp_a,
        json={
            "requesting_institution_id": "hospital-a",
            "resource_type": "diagnostic_slot",
            "quantity": 1,
            "urgency": "routine",
            "required_by_hours": 24,
        },
    )
    assert res.status_code == 200
    prop_decline = res.json()
    decline_prop_id = prop_decline["proposal_id"]
    decline_lock_id = prop_decline.get("lock_id")
    print(f"    [+] Created temporary lock: {decline_lock_id}")

    res = client.post(
        f"/proposals/{decline_prop_id}/authorize",
        headers=headers_hosp_a,
        json={"approved": False, "approved_by": "Coordinator"},
    )
    assert res.status_code == 200
    print("    [+] Proposal declined by coordinator")

    # Verify lock is now released
    res = client.get(f"/locks/{decline_lock_id}")
    assert res.status_code == 200
    lock_status_declined = res.json()["lock"]["status"]
    print(f"    [+] Lock Status on Coral: {lock_status_declined}")
    assert lock_status_declined == "Released"

    # 11. Verify Audit Trail Continuity
    print("\n[8] Verifying Tamper-Evident Audit Trail Integrity...")
    res = client.get("/audit/verify")
    assert res.status_code == 200
    audit_ver = res.json()
    print(f"    [+] Audit Trail Valid: {audit_ver.get('valid')}, Records: {audit_ver.get('total_records')}")
    assert audit_ver.get("valid") is True

    print("\n" + "=" * 75)
    print(" [SUCCESS] FEATURE 3 CAPACITY LOCKING & TWO-PHASE COMMIT VERIFIED!")
    print("=" * 75)

if __name__ == "__main__":
    run_tests()

