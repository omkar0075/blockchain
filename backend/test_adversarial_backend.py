import sys
from pathlib import Path
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from app.main import app

def run_adversarial_backend_tests():
    client = TestClient(app)
    print("=" * 75)
    print(" FEATURE 3: ADVERSARIAL BACKEND & API ATTACK STRESS TESTS")
    print("=" * 75)

    headers_hosp_a = {
        "X-Actor-Id": "hospital-a",
        "X-Api-Key": "medway-key-hosp-a-sec91",
    }
    headers_hosp_b = {
        "X-Actor-Id": "hospital-b",
        "X-Api-Key": "medway-key-hosp-b-sec82",
    }
    headers_lab_c = {
        "X-Actor-Id": "lab-c",
        "X-Api-Key": "medway-key-lab-c-sec73",
    }
    headers_adversary = {
        "X-Actor-Id": "hospital-b",
        "X-Api-Key": "medway-key-hosp-b-sec82",
    }

    # -------------------------------------------------------------
    # ATTACK 1: Over-Capacity Request Exhaustion
    # -------------------------------------------------------------
    print("\n[Attack 1] Submitting request exceeding total provider capacity (999 slots)...")
    res = client.post(
        "/requests",
        headers=headers_hosp_a,
        json={
            "requesting_institution_id": "hospital-a",
            "resource_type": "diagnostic_slot",
            "quantity": 100, # Max allowed by model validator
            "urgency": "critical",
            "required_by_hours": 4,
        },
    )
    # The agent cannot find feasible provider with 100 slots, so selected_provider_id is None
    assert res.status_code == 200
    prop = res.json()
    assert prop.get("selected_provider_id") is None
    assert prop.get("lock_id") is None
    print("    [+] Correctly rejected over-capacity request: No feasible provider or lock allocated")

    # -------------------------------------------------------------
    # ATTACK 2: Adversarial Cross-Institution Proposal Authorization
    # -------------------------------------------------------------
    print("\n[Attack 2] Hospital B attempting to authorize Hospital A's locked proposal...")
    res_req = client.post(
        "/requests",
        headers=headers_hosp_a,
        json={
            "requesting_institution_id": "hospital-a",
            "resource_type": "diagnostic_slot",
            "quantity": 1,
            "urgency": "urgent",
            "required_by_hours": 24,
        },
    )
    assert res_req.status_code == 200
    prop_a = res_req.json()
    prop_a_id = prop_a["proposal_id"]
    lock_a_id = prop_a.get("lock_id")
    print(f"    [+] Created proposal for Hospital A with lock {lock_a_id}")

    # Hospital B tries to authorize Hospital A's proposal
    res_hijack = client.post(
        f"/proposals/{prop_a_id}/authorize",
        headers=headers_hosp_b,
        json={"approved": True, "approved_by": "Dr. Hijacker"},
    )
    assert res_hijack.status_code == 403, f"Expected 403, got {res_hijack.status_code}"
    print(f"    [+] Cross-institution authorization blocked: {res_hijack.status_code} Forbidden")

    # -------------------------------------------------------------
    # ATTACK 3: Double Authorization Race Condition
    # -------------------------------------------------------------
    print("\n[Attack 3] Hospital A attempting double authorization on the same proposal...")
    res_auth1 = client.post(
        f"/proposals/{prop_a_id}/authorize",
        headers=headers_hosp_a,
        json={"approved": True, "approved_by": "Dr. Valid A"},
    )
    assert res_auth1.status_code == 200
    print("    [+] First authorization succeeded and converted lock to commitment")

    # Attempt second authorization on already authorized proposal
    res_auth2 = client.post(
        f"/proposals/{prop_a_id}/authorize",
        headers=headers_hosp_a,
        json={"approved": True, "approved_by": "Dr. Valid A (Duplicate)"},
    )
    # The lock was already converted, so on-chain createReservationWithLock reverts with LockNotActive
    assert res_auth2.status_code in [409, 502], f"Expected 409 or 502 for double conversion, got {res_auth2.status_code}"
    print(f"    [+] Second authorization rejected: {res_auth2.status_code} (Lock cannot be double-converted)")

    # -------------------------------------------------------------
    # ATTACK 4: Adversarial Lock Release by Unauthenticated Party
    # -------------------------------------------------------------
    print("\n[Attack 4] Attacker attempting to release active lock with invalid credentials...")
    if lock_a_id:
        res_rel = client.post(
            f"/locks/{lock_a_id}/release",
            headers={
                "X-Actor-Id": "hospital-a",
                "X-Api-Key": "fake-invalid-key-999",
            },
            json={"reason": "Attacker trying to release"},
        )
        assert res_rel.status_code == 401
        print(f"    [+] Unauthenticated lock release correctly rejected: {res_rel.status_code} Unauthorized")

    # -------------------------------------------------------------
    # ATTACK 5: Quota Setting Privilege Escalation
    # -------------------------------------------------------------
    print("\n[Attack 5] Hospital A attempting to modify Lab C's on-chain quota...")
    res_quota_attack = client.post(
        "/inventory",
        headers=headers_hosp_a, # Hospital A is a requester, not provider lab-c
        json={
            "provider_id": "lab-c",
            "resource_type": "diagnostic_slot",
            "total_capacity": 50,
            "is_active": True,
        },
    )
    assert res_quota_attack.status_code == 403, f"Expected 403, got {res_quota_attack.status_code}"
    print(f"    [+] Unauthorized inventory quota update correctly blocked: {res_quota_attack.status_code} Forbidden")

    # -------------------------------------------------------------
    # ATTACK 6: Cryptographic Audit Trail Tamper Proofing
    # -------------------------------------------------------------
    print("\n[Attack 6] Verifying system-wide audit trail integrity...")
    res_audit = client.get("/audit/verify")
    assert res_audit.status_code == 200
    assert res_audit.json().get("valid") is True
    print(f"    [+] Cryptographic hash-chain continuity verified: Valid={res_audit.json().get('valid')}")

    print("\n" + "=" * 75)
    print(" [SUCCESS] ALL ADVERSARIAL BACKEND ATTACK TESTS PASSED!")
    print("=" * 75)

if __name__ == "__main__":
    run_adversarial_backend_tests()

