import asyncio
from datetime import datetime, timedelta, timezone
import threading
from typing import Any, Optional
from uuid import UUID, uuid4
import pytest

from app.agents.context import AgentContext
from app.agents.provider_agent import ProviderAgent
from app.agents.recovery_agent import FailureClassification
from app.db import init_db
from app.llm.client import LLMClient
from app.llm.llm_orchestrator import LLMOrchestrator
from app.llm.provider import MockLLMProvider
from app.schemas.agent_messages import AgentMessage
from app.schemas.provider_offer import ProviderCost, ProviderOffer
from app.schemas.resource_request import (
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)
from app.schemas.verification import VerificationStatus
from app.workflows.reservation_workflow import (
    ReservationWorkflowCoordinator,
    create_full_coordination_registry,
)


class MultiHospitalCapacityLedger:
    """
    Simulates on-chain smart contract capacity allocation, concurrent locking,
    ownership verification, and shared-ledger state consistency.
    """

    def __init__(self, initial_capacities: Optional[dict[str, int]] = None) -> None:
        self._lock = threading.Lock()
        # provider_id -> available capacity
        self.capacities = initial_capacities or {"lab-c": 5, "hospital-b": 10}
        self.locks: dict[str, dict[str, Any]] = {}
        self.reservations: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []

    def lock_capacity(
        self,
        provider_id: str,
        resource_type: str,
        quantity: int,
        requester_id: str,
        duration_seconds: int = 300,
    ) -> dict[str, Any]:
        with self._lock:
            avail = self.capacities.get(provider_id, 0)
            if avail < quantity:
                return {
                    "success": False,
                    "error": f"Execution reverted: Insufficient available capacity (requested {quantity}, available {avail})",
                }

            self.capacities[provider_id] -= quantity
            lock_id = f"lock-{uuid4()}"
            lock_record = {
                "lock_id": lock_id,
                "provider_id": provider_id,
                "requester_id": requester_id,
                "resource_type": resource_type,
                "quantity": quantity,
                "status": "ACTIVE",
                "expires_at": int(datetime.now(timezone.utc).timestamp()) + duration_seconds,
            }
            self.locks[lock_id] = lock_record
            self.history.append({"action": "LOCK", "record": lock_record})
            return {
                "success": True,
                "lockId": lock_id,
                "txHash": f"0x{uuid4().hex}",
                "blockNumber": 1287000 + len(self.history),
            }

    def release_lock(self, lock_id: str, caller_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            rec = self.locks.get(lock_id)
            if not rec:
                return {"success": False, "error": "Lock not found"}
            if caller_id and rec["requester_id"] != caller_id:
                return {"success": False, "error": "Unauthorized: caller is not the lock owner"}
            if rec["status"] != "ACTIVE":
                return {"success": False, "error": f"Lock is already {rec['status']}"}

            rec["status"] = "RELEASED"
            self.capacities[rec["provider_id"]] += rec["quantity"]
            self.history.append({"action": "RELEASE", "record": rec})
            return {"success": True, "txHash": f"0x{uuid4().hex}"}

    def create_reservation_with_lock(
        self,
        lock_id: str,
        requester_id: str,
        **kwargs,
    ) -> dict[str, Any]:
        with self._lock:
            rec = self.locks.get(lock_id)
            if not rec:
                return {"success": False, "error": "Lock does not exist"}
            if rec["status"] != "ACTIVE":
                return {"success": False, "error": f"Lock is not active: {rec['status']}"}
            if rec["requester_id"] != requester_id:
                return {"success": False, "error": "Cross-institutional ownership violation: caller does not own lock"}

            rec["status"] = "COMMITTED"
            res_id = f"res-{uuid4()}"
            res_record = {
                "reservation_id": res_id,
                "lock_id": lock_id,
                "requester_id": requester_id,
                "provider_id": rec["provider_id"],
                "quantity": rec["quantity"],
                "status": "COMMITTED",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.reservations[res_id] = res_record
            self.history.append({"action": "RESERVE", "record": res_record})
            return {
                "success": True,
                "reservationId": res_id,
                "txHash": f"0x{uuid4().hex}",
                "blockNumber": 1287000 + len(self.history),
            }

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        with self._lock:
            res = self.reservations.get(reservation_id)
            if not res:
                return {"success": False, "error": "Reservation not found"}
            return {"success": True, "status": res["status"], "reservation": dict(res)}

    def get_capacity(self, provider_id: str) -> int:
        with self._lock:
            return self.capacities.get(provider_id, 0)


class MultiHospitalBlockchainClient:
    """Adapter bridging coordinator to the shared multi-hospital ledger."""

    def __init__(self, ledger: MultiHospitalCapacityLedger) -> None:
        self.ledger = ledger

    def lock_capacity(self, **kwargs) -> dict[str, Any]:
        p_id = kwargs.get("provider_id") or "lab-c"
        r_type = kwargs.get("resource_type") or "diagnostic_slot"
        qty = int(kwargs.get("quantity") or 1)
        req_id = kwargs.get("requester_id") or "hospital-a"
        return self.ledger.lock_capacity(p_id, r_type, qty, req_id)

    def create_reservation_with_lock(self, **kwargs) -> dict[str, Any]:
        lock_id = kwargs.get("lock_id") or ""
        req_id = kwargs.get("requester_id") or "hospital-a"
        return self.ledger.create_reservation_with_lock(lock_id, req_id)

    def create_reservation(self, **kwargs) -> dict[str, Any]:
        return self.create_reservation_with_lock(**kwargs)


class MultiHospitalBlockchainReader:
    """Simulates independent read queries against shared ledger."""

    def __init__(self, ledger: MultiHospitalCapacityLedger) -> None:
        self.ledger = ledger

    def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any]:
        return {"success": True, "status": "confirmed", "blockNumber": 1287050, "gasUsed": 95000}

    def get_lock(self, lock_id: str) -> dict[str, Any]:
        rec = self.ledger.locks.get(lock_id)
        if not rec:
            return {"success": False, "error": "Lock not found"}
        return {"success": True, "status": rec["status"], "expiresAt": rec["expires_at"]}

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        return self.ledger.get_reservation(reservation_id)


def create_sample_request(quantity: int = 3, institution_id: str = "hospital-a") -> ResourceRequest:
    return ResourceRequest(
        workflow_id=uuid4(),
        requester_institution_id=institution_id,
        resource_type=ResourceType.DIAGNOSTIC_SLOT,
        quantity=quantity,
        urgency=UrgencyLevel.URGENT,
        location=LocationArea(city="Metropolis"),
        required_by=datetime.now(timezone.utc) + timedelta(hours=12),
        required_by_hours=12,
    )


# -----------------------------------------------------------------------------
# Scenario 1: Two hospitals, one provider
# -----------------------------------------------------------------------------
def test_two_hospitals_one_provider():
    async def run():
        ledger = MultiHospitalCapacityLedger({"lab-c": 10})
        bc_client = MultiHospitalBlockchainClient(ledger)
        bc_reader = MultiHospitalBlockchainReader(ledger)

        coord_a = ReservationWorkflowCoordinator(blockchain_client=bc_client, blockchain_reader=bc_reader)
        coord_b = ReservationWorkflowCoordinator(blockchain_client=bc_client, blockchain_reader=bc_reader)

        now = datetime.now(timezone.utc)
        req_a = ResourceRequest(
            workflow_id=uuid4(),
            requester_institution_id="hospital-a",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=2,
            urgency=UrgencyLevel.URGENT,
            location=LocationArea(city="Metropolis"),
            required_by=now + timedelta(hours=12),
            required_by_hours=12,
        )
        req_b = ResourceRequest(
            workflow_id=uuid4(),
            requester_institution_id="hospital-b",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=3,
            urgency=UrgencyLevel.URGENT,
            location=LocationArea(city="Metropolis"),
            required_by=now + timedelta(hours=12),
            required_by_hours=12,
        )

        res_a = await coord_a.start(req_a)
        res_b = await coord_b.start(req_b)

        assert res_a.selected_offer.provider_id == "lab-c"
        assert res_b.selected_offer.provider_id == "lab-c"

        commit_a = await coord_a.authorize_and_commit(res_a.workflow_id, approved_by="Dr. A")
        commit_b = await coord_b.authorize_and_commit(res_b.workflow_id, approved_by="Dr. B")

        assert commit_a.is_committed is True
        assert commit_b.is_committed is True
        assert commit_a.request.requester_institution_id == "hospital-a"
        assert commit_b.request.requester_institution_id == "hospital-b"
        assert ledger.get_capacity("lab-c") == 5  # 10 - 2 - 3 = 5

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 2: Competing requests for limited capacity (no overbooking)
# -----------------------------------------------------------------------------
def test_competing_requests_limited_capacity():
    async def run():
        # Lab C has strictly 5 available slots
        ledger = MultiHospitalCapacityLedger({"lab-c": 5})
        bc_client = MultiHospitalBlockchainClient(ledger)
        bc_reader = MultiHospitalBlockchainReader(ledger)

        coord = ReservationWorkflowCoordinator(blockchain_client=bc_client, blockchain_reader=bc_reader)

        # Hospital A requests 3 slots (succeeds)
        lock_res_a = bc_client.lock_capacity(provider_id="lab-c", resource_type="diagnostic_slot", quantity=3, requester_id="hospital-a")
        assert lock_res_a["success"] is True

        # Hospital B attempts to request 4 slots (exceeds remaining 2 slots -> MUST revert)
        lock_res_b = bc_client.lock_capacity(provider_id="lab-c", resource_type="diagnostic_slot", quantity=4, requester_id="hospital-b")
        assert lock_res_b["success"] is False
        assert "Insufficient available capacity" in lock_res_b["error"]

        # Exactly 2 slots remain available; exactly 3 slots locked
        assert ledger.get_capacity("lab-c") == 2

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 3: Simultaneous concurrent capacity lock requests
# -----------------------------------------------------------------------------
def test_simultaneous_concurrent_requests():
    async def run():
        ledger = MultiHospitalCapacityLedger({"lab-c": 5})
        bc_client = MultiHospitalBlockchainClient(ledger)

        # 4 hospitals concurrently request 2 slots each (total 8 slots requested, only 5 available)
        results = []

        def worker(req_id: str):
            res = bc_client.lock_capacity(provider_id="lab-c", resource_type="diagnostic_slot", quantity=2, requester_id=req_id)
            results.append((req_id, res["success"]))

        threads = [threading.Thread(target=worker, args=(f"hospital-{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[1] is True]
        failures = [r for r in results if r[1] is False]

        # Only 2 requests of 2 slots can fit in 5 slots (total allocated 4 slots)
        assert len(successes) == 2
        assert len(failures) == 2
        assert ledger.get_capacity("lab-c") == 1  # 5 - 4 = 1

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 4: Institutional isolation (cannot hijack context or lock)
# -----------------------------------------------------------------------------
def test_institutional_isolation():
    async def run():
        ledger = MultiHospitalCapacityLedger({"lab-c": 5})
        bc_client = MultiHospitalBlockchainClient(ledger)

        # Hospital A locks capacity
        lock_a = ledger.lock_capacity(provider_id="lab-c", resource_type="diagnostic_slot", quantity=2, requester_id="hospital-a")
        lock_id = lock_a["lockId"]

        # Hospital B attempts to commit using Hospital A's lock
        res_hijack = ledger.create_reservation_with_lock(lock_id=lock_id, requester_id="hospital-b")
        assert res_hijack["success"] is False
        assert "ownership violation" in res_hijack["error"].lower()

        # Context isolation: AgentContext enforces institution ID boundary
        ctx_a = AgentContext(workflow_id=uuid4(), request=create_sample_request(2), current_agent="AgentA")
        assert ctx_a.request.requester_institution_id == "hospital-a"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 5: Multi-provider selection based on constraints
# -----------------------------------------------------------------------------
def test_provider_selection_based_on_constraints():
    async def run():
        ledger = MultiHospitalCapacityLedger({"hospital-b": 10, "lab-c": 10})
        coord = ReservationWorkflowCoordinator(
            blockchain_client=MultiHospitalBlockchainClient(ledger),
            blockchain_reader=MultiHospitalBlockchainReader(ledger),
        )

        now = datetime.now(timezone.utc)
        # Fast deadline: 2 hours. Hospital B offers 4h delivery, Lab C offers 2h delivery.
        req_urgent = ResourceRequest(
            workflow_id=uuid4(),
            requester_institution_id="hospital-a",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=2,
            urgency=UrgencyLevel.CRITICAL,
            location=LocationArea(city="Metropolis"),
            required_by=now + timedelta(hours=2),
            required_by_hours=2,
        )

        res = await coord.start(req_urgent)
        # Lab C must be chosen because Hospital B's 4h exceeds 2h deadline
        assert res.selected_offer.provider_id == "lab-c"
        assert res.selected_offer.estimated_delivery_hours <= 2

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 6: Partial fulfillment without exceeding requested quantity
# -----------------------------------------------------------------------------
def test_partial_fulfillment_boundary():
    async def run():
        # NegotiationAgent with partial offer
        req = create_sample_request(quantity=5)
        now = datetime.now(timezone.utc)

        offer_partial = ProviderOffer(
            offer_id=uuid4(),
            request_id=uuid4(),
            provider_id="lab-c",
            provider_name="Lab C",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            offered_quantity=3,  # only 3 available
            location=LocationArea(city="Metropolis"),
            cost=ProviderCost(amount=120.0, currency="USD"),
            estimated_delivery_hours=2,
            available_from=now,
            available_until=now + timedelta(hours=24),
            expires_at=now + timedelta(hours=6),
        )

        coord = ReservationWorkflowCoordinator()
        neg = coord.registry.get("NegotiationAgent")
        msg = AgentMessage(
            workflow_id=req.workflow_id,
            sender="Orchestrator",
            receiver="NegotiationAgent",
            message_type="NEGOTIATE_OFFERS",
            payload={"resource_request": req.model_dump(), "offers": [offer_partial.model_dump()]},
        )
        res = await neg.execute(msg)

        # Counteroffer generated for partial quantity (3)
        assert res.data["is_counteroffer"] is True
        assert res.data["proposal"]["proposed_quantity"] == 3
        assert res.data["proposal"]["proposed_quantity"] <= req.quantity

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 7: Provider failure releases capacity correctly
# -----------------------------------------------------------------------------
def test_provider_failure_releases_capacity():
    async def run():
        ledger = MultiHospitalCapacityLedger({"lab-c": 5})

        # Lock 3 slots
        lock_res = ledger.lock_capacity(provider_id="lab-c", resource_type="diagnostic_slot", quantity=3, requester_id="hospital-a")
        lock_id = lock_res["lockId"]
        assert ledger.get_capacity("lab-c") == 2

        # Provider fails or transaction aborts -> release lock
        rel_res = ledger.release_lock(lock_id, caller_id="hospital-a")
        assert rel_res["success"] is True

        # Capacity is restored back to 5
        assert ledger.get_capacity("lab-c") == 5

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 8: Cross-institution audit attribution
# -----------------------------------------------------------------------------
def test_cross_institution_audit_attribution():
    async def run():
        ledger = MultiHospitalCapacityLedger({"lab-c": 10})
        bc_client = MultiHospitalBlockchainClient(ledger)
        bc_reader = MultiHospitalBlockchainReader(ledger)

        coord = ReservationWorkflowCoordinator(blockchain_client=bc_client, blockchain_reader=bc_reader)
        now = datetime.now(timezone.utc)

        req_a = ResourceRequest(
            workflow_id=uuid4(),
            requester_institution_id="hospital-a",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=2,
            urgency=UrgencyLevel.URGENT,
            location=LocationArea(city="Metropolis"),
            required_by=now + timedelta(hours=12),
            required_by_hours=12,
        )

        res = await coord.start(req_a)
        committed = await coord.authorize_and_commit(res.workflow_id, approved_by="Dr. Sarah Chen, Hospital A")

        # Audit events must clearly indicate institution and approver
        assert committed.approved_by == "Dr. Sarah Chen, Hospital A"
        assert committed.request.requester_institution_id == "hospital-a"
        confirm_step = next(s for s in committed.audit_trail if s["to_state"] == "CONFIRMED")
        assert "tx:" in confirm_step["details"]

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 9: Duplicate coordination idempotency
# -----------------------------------------------------------------------------
def test_duplicate_coordination_idempotency():
    async def run():
        ledger = MultiHospitalCapacityLedger({"lab-c": 5})
        bc_client = MultiHospitalBlockchainClient(ledger)
        bc_reader = MultiHospitalBlockchainReader(ledger)

        coord = ReservationWorkflowCoordinator(blockchain_client=bc_client, blockchain_reader=bc_reader)
        req = create_sample_request(2)

        await coord.start(req, workflow_id=req.workflow_id)

        # Retry with exact same workflow ID must reject duplicate execution
        with pytest.raises(ValueError, match="already active or completed"):
            await coord.start(req, workflow_id=req.workflow_id)

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 10: Shared ledger consistency across independent institutions
# -----------------------------------------------------------------------------
def test_shared_ledger_consistency():
    async def run():
        ledger = MultiHospitalCapacityLedger({"lab-c": 10})
        reader_a = MultiHospitalBlockchainReader(ledger)
        reader_b = MultiHospitalBlockchainReader(ledger)

        # Lock and reserve
        lock = ledger.lock_capacity("lab-c", "diagnostic_slot", 3, "hospital-a")
        res = ledger.create_reservation_with_lock(lock["lockId"], "hospital-a")
        res_id = res["reservationId"]

        # Hospital A and Hospital B independently read the same reservation from the ledger
        view_a = reader_a.get_reservation(res_id)
        view_b = reader_b.get_reservation(res_id)

        assert view_a["success"] is True
        assert view_b["success"] is True
        assert view_a["reservation"]["status"] == "COMMITTED"
        assert view_b["reservation"]["status"] == "COMMITTED"
        assert view_a["reservation"]["quantity"] == 3
        assert view_b["reservation"]["quantity"] == 3
        assert view_a["reservation"] == view_b["reservation"]

    asyncio.run(run())
