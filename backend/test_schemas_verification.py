from datetime import datetime, timezone
from uuid import UUID, uuid4
import pytest

from app.schemas.verification import (
    FailedCheck,
    VerificationChecklist,
    VerificationResult,
    VerificationStatus,
)


def test_valid_verified_result():
    """Verify clean instantiation of a fully VERIFIED snapshot where all checks pass."""
    wf_id = uuid4()
    offer_id = uuid4()
    req_id = uuid4()

    checks = VerificationChecklist(
        institution_registered=True,
        institution_active=True,
        wallet_authorized=True,
        resource_registered=True,
        offer_unexpired=True,
        capacity_available=True,
        request_compatible=True,
    )

    result = VerificationResult(
        workflow_id=wf_id,
        provider_id="lab-c",
        offer_id=offer_id,
        request_id=req_id,
        status=VerificationStatus.VERIFIED,
        checks=checks,
        failed_checks=[],
        explanation="All 7 safety, cryptographic, and on-chain capacity checks verified on Coral Testnet.",
        onchain_block_number=405600,
    )

    assert isinstance(result.verification_id, UUID)
    assert result.status == VerificationStatus.VERIFIED
    assert result.checks.is_all_passed() is True
    assert len(result.failed_checks) == 0
    assert result.onchain_block_number == 405600
    assert result.verified_at.tzinfo == timezone.utc


def test_rejected_result_with_failed_checks():
    """Verify REJECTED status with structured machine-readable reason codes."""
    wf_id = uuid4()
    offer_id = uuid4()

    checks = VerificationChecklist(
        institution_registered=True,
        institution_active=True,
        wallet_authorized=False,  # Failed
        resource_registered=True,
        offer_unexpired=True,
        capacity_available=False,  # Failed
        request_compatible=True,
    )

    failed_checks = [
        FailedCheck(
            check_name="wallet_authorized",
            reason_code="ERR_WALLET_UNAUTHORIZED",
            message="Signer wallet does not match registered institutional provider address.",
        ),
        FailedCheck(
            check_name="capacity_available",
            reason_code="ERR_CAPACITY_DEPLETED",
            message="On-chain smart contract capacity report shows 0 available uncommitted slots.",
        ),
    ]

    result = VerificationResult(
        workflow_id=wf_id,
        provider_id="unverified-clinic",
        offer_id=offer_id,
        status=VerificationStatus.REJECTED,
        checks=checks,
        failed_checks=failed_checks,
        explanation="Provider failed wallet authorization and on-chain capacity availability checks.",
    )

    assert result.status == VerificationStatus.REJECTED
    assert result.checks.is_all_passed() is False
    assert len(result.failed_checks) == 2
    assert result.failed_checks[0].reason_code == "ERR_WALLET_UNAUTHORIZED"
    assert result.failed_checks[1].reason_code == "ERR_CAPACITY_DEPLETED"


def test_inconsistent_verified_status_rejected():
    """Verify status cannot be VERIFIED if any check failed or failed_checks is populated."""
    wf_id = uuid4()
    offer_id = uuid4()

    # 1. Status VERIFIED, but checklist has False
    failing_checks = VerificationChecklist(
        institution_registered=True,
        institution_active=True,
        wallet_authorized=True,
        resource_registered=True,
        offer_unexpired=True,
        capacity_available=False,  # Failure
        request_compatible=True,
    )
    with pytest.raises(ValueError, match="status cannot be VERIFIED when one or more individual checks failed"):
        VerificationResult(
            workflow_id=wf_id,
            provider_id="lab-c",
            offer_id=offer_id,
            status=VerificationStatus.VERIFIED,
            checks=failing_checks,
        )

    # 2. Status VERIFIED, all checks True, but failed_checks has entries
    passing_checks = VerificationChecklist(
        institution_registered=True,
        institution_active=True,
        wallet_authorized=True,
        resource_registered=True,
        offer_unexpired=True,
        capacity_available=True,
        request_compatible=True,
    )
    with pytest.raises(ValueError, match="status cannot be VERIFIED when failed_checks list is non-empty"):
        VerificationResult(
            workflow_id=wf_id,
            provider_id="lab-c",
            offer_id=offer_id,
            status=VerificationStatus.VERIFIED,
            checks=passing_checks,
            failed_checks=[
                FailedCheck(check_name="test", reason_code="ERR_TEST", message="Test failure")
            ],
        )


def test_inconsistent_rejected_status_rejected():
    """Verify status cannot be REJECTED if all checks actually passed with no errors."""
    passing_checks = VerificationChecklist(
        institution_registered=True,
        institution_active=True,
        wallet_authorized=True,
        resource_registered=True,
        offer_unexpired=True,
        capacity_available=True,
        request_compatible=True,
    )
    with pytest.raises(ValueError, match="status cannot be REJECTED when all checks passed"):
        VerificationResult(
            workflow_id=uuid4(),
            provider_id="lab-c",
            offer_id=uuid4(),
            status=VerificationStatus.REJECTED,
            checks=passing_checks,
            failed_checks=[],
        )


def test_invalid_identifiers_rejected():
    """Verify empty provider_id and failed_check fields are rejected."""
    with pytest.raises(ValueError, match="provider_id cannot be empty or whitespace only"):
        VerificationResult(
            workflow_id=uuid4(),
            provider_id="   ",
            offer_id=uuid4(),
            status=VerificationStatus.PENDING,
        )

    with pytest.raises(ValueError):
        FailedCheck(check_name="", reason_code="CODE", message="desc")

    with pytest.raises(ValueError, match="Field cannot be empty or whitespace only"):
        FailedCheck(check_name="   ", reason_code="CODE", message="desc")


def test_timezone_handling():
    """Verify naive timestamp is rejected."""
    now_naive = datetime.now()

    with pytest.raises(ValueError, match="verified_at timestamp must be timezone-aware"):
        VerificationResult(
            workflow_id=uuid4(),
            provider_id="lab-c",
            offer_id=uuid4(),
            status=VerificationStatus.PENDING,
            verified_at=now_naive,
        )


def test_pii_strictly_rejected():
    """Verify rejection of patient identifying fields."""
    forbidden = [
        {"patient_name": "Bob Taylor"},
        {"patient_id": "PT-7788"},
        {"ssn": "000-11-2222"},
        {"mrn": "MRN-101"},
    ]

    for pii in forbidden:
        payload = {
            "workflow_id": str(uuid4()),
            "provider_id": "lab-c",
            "offer_id": str(uuid4()),
            "status": "PENDING",
            **pii,
        }
        with pytest.raises(ValueError, match="Privacy violation: patient identifying fields are strictly forbidden"):
            VerificationResult.model_validate(payload)


def test_requires_review_status():
    """Verify REQUIRES_REVIEW status for ambiguous cases (e.g. pending manual authorization)."""
    checks = VerificationChecklist(
        institution_registered=True,
        institution_active=True,
        wallet_authorized=True,
        resource_registered=True,
        offer_unexpired=True,
        capacity_available=True,
        request_compatible=False,
    )
    result = VerificationResult(
        workflow_id=uuid4(),
        provider_id="hospital-b",
        offer_id=uuid4(),
        status=VerificationStatus.REQUIRES_REVIEW,
        checks=checks,
        failed_checks=[
            FailedCheck(
                check_name="request_compatible",
                reason_code="MANUAL_AUTHORIZATION_REQUIRED",
                message="Resource requires specialized medical board authorization.",
            )
        ],
    )
    assert result.status == VerificationStatus.REQUIRES_REVIEW


def test_json_roundtrip_serialization():
    """Verify JSON serialization and deserialization roundtrip."""
    wf_id = uuid4()
    offer_id = uuid4()

    checks = VerificationChecklist(
        institution_registered=True,
        institution_active=True,
        wallet_authorized=True,
        resource_registered=True,
        offer_unexpired=True,
        capacity_available=True,
        request_compatible=True,
    )

    result = VerificationResult(
        workflow_id=wf_id,
        provider_id="lab-c",
        offer_id=offer_id,
        status=VerificationStatus.VERIFIED,
        checks=checks,
        explanation="Point-in-time capacity verified at block 405600",
        onchain_block_number=405600,
    )

    json_str = result.model_dump_json()
    assert "VERIFIED" in json_str
    assert "lab-c" in json_str
    assert "405600" in json_str

    restored = VerificationResult.model_validate_json(json_str)
    assert restored.verification_id == result.verification_id
    assert restored.status == VerificationStatus.VERIFIED
    assert restored.checks.is_all_passed() is True
    assert restored.onchain_block_number == 405600
    assert restored.verified_at.tzinfo == timezone.utc
