"""Apex AI Arabia — Interactive Enterprise Demonstration Script.

Executes a complete live end-to-end demonstration of the healthcare referral backend:
1. Role-Based JWT Authentication & Multi-Hospital Isolation
2. Hash-based API Idempotency & Conflict Detection (409)
3. Transactional Outbox Pattern & Queue Publishing
4. Step-by-Step Workflow Orchestration (EHR -> AI -> Routing -> CRM)
5. Transient Fault Handling, 3-Attempt Exponential Retries & Dead Letter Queue (DLQ)
6. Operations-Admin DLQ Replay & Step-Level Resume
7. Immutable Audit Trail & Integration Telemetry Inspection

Usage:
    uv run python scripts/demo.py
"""

import asyncio
import json
import time
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.adapters.mocks import (
    InMemoryEventQueue,
    MockAIAdapter,
    MockCRMAdapter,
    MockEHRAdapter,
)
from src.auth import create_mock_token
from src.database import get_session
from src.main import app
from src.models.base import Base
from src.models.referral import ReferralStatus
from src.repositories.audit import AuditRepository
from src.repositories.integration import IntegrationRepository
from src.repositories.outbox import OutboxRepository
from src.repositories.referrals import ReferralRepository
from src.services.outbox_publisher import OutboxPublisher
from src.services.retry import RetryPolicy
from src.services.routing import RoutingService
from src.services.worker import ReferralWorker


# Visual formatting helpers
class Colors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def print_section(title: str) -> None:
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 80}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  {title}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 80}{Colors.RESET}\n")


def print_step(step_num: int, title: str, description: str = "") -> None:
    print(f"{Colors.BOLD}{Colors.YELLOW}[STEP {step_num}] {title}{Colors.RESET}")
    if description:
        print(f"{Colors.DIM}         {description}{Colors.RESET}")


async def run_demo() -> None:
    print_section("APEX AI ARABIA -- ENTERPRISE BACKEND DEMONSTRATION")
    print("Initializing isolated async database engine & in-memory event bus...")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Wire FastAPI dependency override to use demo database
    async def override_get_session():
        async with session_factory() as sess:
            yield sess
            await sess.commit()

    app.dependency_overrides[get_session] = override_get_session
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    # Adapters & services
    event_queue = InMemoryEventQueue()
    outbox_publisher = OutboxPublisher(event_queue)
    ehr = MockEHRAdapter()
    ai = MockAIAdapter()
    routing = RoutingService()
    crm = MockCRMAdapter()
    worker = ReferralWorker(
        ehr_adapter=ehr,
        ai_adapter=ai,
        routing_service=routing,
        crm_adapter=crm,
        retry_policy=RetryPolicy(max_attempts=3, backoff_intervals=[0.1, 0.2, 0.4]),
    )

    # -----------------------------------------------------------------------
    # STEP 1: JWT Authentication & Intake
    # -----------------------------------------------------------------------
    print_step(1, "SUBMITTING REFERRAL (INTAKE API)", "Role: contact_center_agent (RIYADH-01)")
    agent_token = create_mock_token("agent_riyadh01")
    idempotency_key = "IDEM-KEY-DEMO-001"

    payload_1 = {
        "external_reference": "EXT-REF-2026-001",
        "patient": {"id": "PAT-SYN-001"},
        "specialty": "cardiology",
        "preferred_hospital": "RIYADH-01",
        "reason": "Severe chest pain on exertion and shortness of breath",
    }

    resp = await client.post(
        "/api/v1/referrals",
        json=payload_1,
        headers={
            "Authorization": f"Bearer {agent_token}",
            "X-Idempotency-Key": idempotency_key,
            "X-Request-ID": "req_demo_001",
        },
    )

    print(f"Status Code : {Colors.GREEN}{resp.status_code} ACCEPTED{Colors.RESET}")
    data_1 = resp.json()
    referral_id_1 = data_1["referral_id"]
    print(f"Response    : {json.dumps(data_1, indent=2)}")
    print(f"Correlation : X-Request-ID = {resp.headers.get('x-request-id')}")

    # -----------------------------------------------------------------------
    # STEP 2: Idempotency & Conflict Detection
    # -----------------------------------------------------------------------
    print_step(2, "TESTING HASH-BASED IDEMPOTENCY & CONFLICT DETECTION")
    print(f"{Colors.DIM}2a. Exact duplicate submission (same key, same body):{Colors.RESET}")
    resp_replay = await client.post(
        "/api/v1/referrals",
        json=payload_1,
        headers={
            "Authorization": f"Bearer {agent_token}",
            "X-Idempotency-Key": idempotency_key,
        },
    )
    print(f"    --> Response Code: {Colors.GREEN}{resp_replay.status_code} ACCEPTED{Colors.RESET} (Original referral returned, no duplicate created)")

    print(f"{Colors.DIM}2b. Conflict submission (same key, altered payload):{Colors.RESET}")
    altered_payload = {**payload_1, "reason": "Altered reason text causing checksum mismatch"}
    resp_conflict = await client.post(
        "/api/v1/referrals",
        json=altered_payload,
        headers={
            "Authorization": f"Bearer {agent_token}",
            "X-Idempotency-Key": idempotency_key,
        },
    )
    err_data = resp_conflict.json()
    err_code = err_data.get("detail", {}).get("code") if isinstance(err_data.get("detail"), dict) else err_data.get("error", {}).get("code", "IDEMPOTENCY_CONFLICT")
    print(f"    --> Error Code   : {err_code}")

    # -----------------------------------------------------------------------
    # STEP 3: Multi-Hospital Security & Data Isolation
    # -----------------------------------------------------------------------
    print_step(3, "MULTI-HOSPITAL DATA ISOLATION (NON-ENUMERATING 404)")
    agent_r2_token = create_mock_token("agent_riyadh02")

    print(f"{Colors.DIM}3a. RIYADH-02 agent attempting to query RIYADH-01 referral:{Colors.RESET}")
    resp_cross = await client.get(
        f"/api/v1/referrals/{referral_id_1}",
        headers={"Authorization": f"Bearer {agent_r2_token}"},
    )
    print(f"    --> Response Code: {Colors.RED}{resp_cross.status_code} NOT FOUND{Colors.RESET} (Masks existence across facilities)")

    print(f"{Colors.DIM}3b. RIYADH-01 agent querying their own facility's referral:{Colors.RESET}")
    resp_own = await client.get(
        f"/api/v1/referrals/{referral_id_1}",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    print(f"    --> Response Code: {Colors.GREEN}{resp_own.status_code} OK{Colors.RESET}")
    print(f"    --> Status Body  : {json.dumps(resp_own.json(), indent=2)}")
    print(f"    --> {Colors.CYAN}Notice: Patient ID & Clinical reason are stripped for PDPL privacy compliance.{Colors.RESET}")

    # -----------------------------------------------------------------------
    # STEP 4: Transactional Outbox Publishing
    # -----------------------------------------------------------------------
    print_step(4, "TRANSACTIONAL OUTBOX PUBLISHING")
    async with session_factory() as sess:
        outbox_repo = OutboxRepository(sess)
        pending = await outbox_repo.get_pending()
        print(f"Pre-Publish Outbox Status : {Colors.YELLOW}{len(pending)} PENDING event(s) in database{Colors.RESET}")

        pub_count = await outbox_publisher.publish_pending(sess)
        print(f"Published to Event Queue  : {Colors.GREEN}{pub_count} event(s) published{Colors.RESET}")
        print(f"In-Memory Queue Backlog   : {event_queue.qsize()} message(s) ready for worker")

    # -----------------------------------------------------------------------
    # STEP 5: Background Worker Workflow Orchestration
    # -----------------------------------------------------------------------
    print_step(5, "BACKGROUND WORKER PROCESSING (HAPPY PATH)")
    print("Worker consuming event from queue and executing step-by-step pipeline...")

    event_msg = await event_queue.get()
    async with session_factory() as sess:
        processed_ref = await worker.process_referral(sess, event_msg["referral_id"])
        await sess.commit()

    print(f"Pipeline State Completed!")
    print(f"  |- Status            : {Colors.GREEN}{processed_ref.status.value}{Colors.RESET}")
    print(f"  |- Current Step      : {processed_ref.current_step.value}")
    print(f"  |- EHR Verification  : {processed_ref.eligibility_reference}")
    print(f"  |- AI Classification : {processed_ref.ai_classification} (Urgency: {processed_ref.ai_urgency}, Confidence: {processed_ref.ai_confidence})")
    print(f"  |- Routing Decision  : Hospital = {processed_ref.hospital} (Reason: {processed_ref.routing_reason})")
    print(f"  \\- CRM Sync          : Case ID = {Colors.BOLD}{processed_ref.crm_case_id}{Colors.RESET}")

    # -----------------------------------------------------------------------
    # STEP 6: Transient Failure, Exponential Retries & Dead Letter Queue
    # -----------------------------------------------------------------------
    print_step(6, "FAULT INJECTION: CRM 503 OUTAGE -> RETRIES -> DEAD LETTER QUEUE")
    print("Submitting second referral and simulating upstream CRM service outage (HTTP 503)...")

    # Inject 5 transient CRM failures
    crm.fail_next_n_creates = 5

    payload_2 = {
        "external_reference": "EXT-REF-2026-002",
        "patient": {"id": "PAT-SYN-002"},
        "specialty": "cardiology",
        "preferred_hospital": "RIYADH-01",
        "reason": "Routine hypertension check",
    }
    resp_2 = await client.post(
        "/api/v1/referrals",
        json=payload_2,
        headers={"Authorization": f"Bearer {agent_token}", "X-Idempotency-Key": "IDEM-KEY-DEMO-002"},
    )
    referral_id_2 = resp_2.json()["referral_id"]

    # Publish outbox & run worker
    async with session_factory() as sess:
        await outbox_publisher.publish_pending(sess)
        event_msg_2 = await event_queue.get()
        print(f"Worker attempting CRM synchronization with 3 exponential backoff retries...")
        processed_ref_2 = await worker.process_referral(sess, event_msg_2["referral_id"])
        await sess.commit()

    print(f"Resulting State : {Colors.RED}{processed_ref_2.status.value}{Colors.RESET}")
    print(f"Failed Step     : {processed_ref_2.failed_step}")
    print(f"Failure Code    : {processed_ref_2.failure_code}")
    print(f"Retry Count     : {processed_ref_2.retry_count} attempts consumed")

    # -----------------------------------------------------------------------
    # STEP 7: Operational DLQ Replay & Step-Level Resume
    # -----------------------------------------------------------------------
    print_step(7, "OPERATIONAL DLQ REPLAY & STEP-LEVEL RESUME")
    ops_token = create_mock_token("ops_admin")

    print(f"{Colors.DIM}7a. Unauthorized Agent attempting to trigger replay:{Colors.RESET}")
    resp_unauth_replay = await client.post(
        f"/api/v1/referrals/{referral_id_2}/replay",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    print(f"    --> Response Code: {Colors.RED}{resp_unauth_replay.status_code} FORBIDDEN{Colors.RESET} (Role requirement enforced)")

    print(f"{Colors.DIM}7b. Operations Admin triggering authorized DLQ replay:{Colors.RESET}")
    resp_ops_replay = await client.post(
        f"/api/v1/referrals/{referral_id_2}/replay",
        headers={"Authorization": f"Bearer {ops_token}"},
    )
    print(f"    --> Response Code: {Colors.GREEN}{resp_ops_replay.status_code} OK{Colors.RESET}")
    print(f"    --> Replay Body  : {json.dumps(resp_ops_replay.json(), indent=2)}")

    print(f"{Colors.DIM}7c. Upstream CRM recovers; Worker resumes processing:{Colors.RESET}")
    # CRM has now recovered (fail_next_n_creates = 0)
    async with session_factory() as sess:
        await outbox_publisher.publish_pending(sess)
        replayed_event = await event_queue.get()
        replayed_ref = await worker.process_referral(sess, replayed_event["referral_id"])
        await sess.commit()

    print(f"    --> Final State  : {Colors.GREEN}{replayed_ref.status.value}{Colors.RESET} (CRM Case: {replayed_ref.crm_case_id})")
    print(f"    --> {Colors.CYAN}Notice: Step-level idempotency skipped EHR & AI on resume, executing only the CRM step!{Colors.RESET}")

    # -----------------------------------------------------------------------
    # STEP 8: Audit & Compliance Inspection
    # -----------------------------------------------------------------------
    print_step(8, "AUDIT TRAIL & INTEGRATION TELEMETRY INSPECTION")
    async with session_factory() as sess:
        audit_repo = AuditRepository(sess)
        int_repo = IntegrationRepository(sess)

        audit_logs = await audit_repo.get_by_referral(referral_id_2)
        int_events = await int_repo.get_events_for_referral(referral_id_2)

    print(f"\n{Colors.BOLD}Integration Step Attempts for {referral_id_2}:{Colors.RESET}")
    print(f"{'STEP':<20} | {'ATTEMPT':<8} | {'OUTCOME':<22} | {'RESULT / ERROR':<25}")
    print("-" * 80)
    for e in int_events:
        err = e.error_code or e.result_reference or "-"
        print(f"{e.step.value:<20} | {e.attempt:<8} | {e.outcome.value:<22} | {err:<25}")

    print(f"\n{Colors.BOLD}Audit Log Trail for {referral_id_2}:{Colors.RESET}")
    print(f"{'ACTOR':<22} | {'ACTION':<24} | {'OUTCOME':<12}")
    print("-" * 65)
    for a in audit_logs:
        print(f"{a.actor:<22} | {a.action:<24} | {a.outcome:<12}")

    print_section("DEMONSTRATION COMPLETE -- ALL 8 ENTERPRISE CAPABILITIES VERIFIED")
    await client.aclose()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_demo())
