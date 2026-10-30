import sys
from pathlib import Path
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from app.main import app

def test_api():
    client = TestClient(app)
    print("Testing FastAPI REST API Endpoints...")

    # 1. Health check
    res = client.get("/health")
    assert res.status_code == 200, f"Health failed: {res.text}"
    data = res.json()
    assert data["status"] == "ok"
    assert "contract_address" in data["blockchain"]
    print("  [+] GET /health -> 200 OK (Contract:", data["blockchain"]["contract_address"] + ")")

    # 2. Institutions
    res = client.get("/institutions")
    assert res.status_code == 200, f"Institutions failed: {res.text}"
    insts = res.json()
    print(f"  [+] GET /institutions -> 200 OK ({len(insts)} accredited institutions)")

    # 3. On-chain Institution Check
    res = client.get("/institutions/hospital-a/onchain")
    assert res.status_code == 200, f"Institution lookup failed: {res.text}"
    inst_data = res.json()
    assert inst_data.get("active") == True
    assert inst_data.get("name") == "City Hospital A"
    print(f"  [+] GET /institutions/hospital-a/onchain -> 200 OK ({inst_data.get('name')} | Role: {inst_data.get('role')})")

    # 4. Audit Trail Verification
    res = client.get("/audit/verify")
    assert res.status_code == 200, f"Audit verify failed: {res.text}"
    audit_data = res.json()
    assert audit_data.get("valid") == True
    print(f"  [+] GET /audit/verify -> 200 OK (Hash-chain Valid: {audit_data.get('valid')}, Records: {audit_data.get('total_records')})")

    # 5. Audit Log List
    res = client.get("/audit")
    assert res.status_code == 200, f"Audit failed: {res.text}"
    print(f"  [+] GET /audit -> 200 OK ({len(res.json())} audit records in log)")

    # 6. Invalid API key credentials rejection
    res = client.post(
        "/requests",
        headers={
            "X-Actor-Id": "hospital-a",
            "X-Api-Key": "wrong-secret-key"
        },
        json={
            "requesting_institution_id": "hospital-a",
            "resource_type": "diagnostic_slot",
            "quantity": 2,
            "urgency": "urgent"
        }
    )
    assert res.status_code == 401, f"Expected 401 for wrong api key, got {res.status_code}"
    print(f"  [+] POST /requests with invalid API key correctly rejected -> {res.status_code} Unauthorized")

    # 7. Institution spoofing rejection
    res = client.post(
        "/requests",
        headers={
            "X-Actor-Id": "hospital-a",
            "X-Api-Key": "medway-key-hosp-a-sec91"
        },
        json={
            "requesting_institution_id": "hospital-b",
            "resource_type": "diagnostic_slot",
            "quantity": 2,
            "urgency": "urgent"
        }
    )
    assert res.status_code == 403, f"Expected 403 for spoofed institution ID, got {res.status_code}"
    print(f"  [+] POST /requests with spoofed institution ID correctly rejected -> {res.status_code} Forbidden")

    # 8. Authenticated Request
    res = client.post(
        "/requests",
        headers={
            "X-Actor-Id": "hospital-a",
            "X-Api-Key": "medway-key-hosp-a-sec91"
        },
        json={
            "requesting_institution_id": "hospital-a",
            "resource_type": "diagnostic_slot",
            "quantity": 2,
            "urgency": "urgent"
        }
    )
    assert res.status_code == 200, f"Authenticated request failed: {res.text}"
    req_data = res.json()
    assert req_data["request"]["requesting_institution_id"] == "hospital-a"
    print(f"  [+] POST /requests with Hospital A credentials -> 200 OK (Selected Provider: {req_data.get('selected_provider_id')})")

    # 8. Commitments endpoint
    res = client.get("/commitments")
    assert res.status_code == 200
    print(f"  [+] GET /commitments -> 200 OK ({len(res.json())} commitments found)")

    print("\nALL FASTAPI REST ENDPOINTS PASSED VERIFICATION!")

if __name__ == "__main__":
    test_api()
