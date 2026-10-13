# Operations Runbook: Referral Integration Service

## 1. Overview & Purpose
This runbook provides operational guidelines, incident response procedures, and triage workflows for the **Apex AI Arabia Healthcare Referral Integration Service**.

---

## 2. Service Architecture & Dependencies

| Dependency | Purpose | Failure Mode | Impact |
| :--- | :--- | :--- | :--- |
| **PostgreSQL 16** | Core operational state, transactional outbox, audit logs | Connection drop / disk full | API returns 500, transactions roll back safely |
| **Redis 7.4** | Shared API token buckets | Connection loss / saturation | API fails open, logs an alert, and relies on gateway limits |
| **EHR Service** | Insurance & clinical eligibility check | 504 Timeout / 500 Server Error | Worker retries 3x $\rightarrow$ transitions to `DEAD_LETTER` |
| **AI Triage Service** | Advisory urgency & specialty classification | Slow response / 422 Invalid payload | Worker retries or moves to `DEAD_LETTER` |
| **CRM System** | External case / appointment booking | 503 Service Unavailable | Worker retries 3x with backoff $\rightarrow$ transitions to `DEAD_LETTER` |

---

## 3. Incident Triage: Dead Letter Queue (DLQ)

### 3.1 What is a Dead Letter Referral?
A referral enters `status = "DEAD_LETTER"` when:
1. A transient technical error (5xx, 429, timeout) exhausts all **3 retry attempts** (exponential backoff: 500ms $\rightarrow$ 1s $\rightarrow$ 2s).
2. A non-retryable technical error (400 Bad Request, malformed vendor response) occurs.

> **Note**: An insurance ineligibility response is a *business outcome*, not a technical failure. Ineligible patients transition cleanly to `ELIGIBILITY_FAILED` and do **not** enter the Dead Letter Queue.

---

### 3.2 Identification & Diagnostic Queries

Connect to PostgreSQL and inspect failed referrals:

```sql
-- 1. Identify all referrals currently in DEAD_LETTER state
SELECT 
    referral_id,
    external_reference,
    failed_step,
    failure_code,
    retry_count,
    created_at,
    updated_at
FROM referrals
WHERE status = 'DEAD_LETTER'
ORDER BY updated_at DESC;

-- 2. Inspect step-by-step integration history for a specific referral
SELECT 
    step,
    attempt,
    outcome,
    error_code,
    error_message,
    result_reference,
    started_at,
    completed_at
FROM integration_events
WHERE referral_id = 'ref_<ID>'
ORDER BY id ASC;

-- 3. Review audit trail for correlation and security tracking
SELECT 
    timestamp,
    actor,
    action,
    outcome,
    request_id,
    details
FROM audit_logs
WHERE resource_id = 'ref_<ID>'
ORDER BY id ASC;
```

---

## 4. Remediation & Authorized DLQ Replay

### 4.1 Replay Policy & RBAC Gate
* **Authorization**: Only users with the `operations_admin` role can trigger replays.
* **Safety & Deduplication**: Replays leverage **step-level idempotency**. The worker checks `integration_events` and automatically skips already-completed steps (e.g. EHR and AI), resuming execution directly at the failed step (e.g. CRM).

### 4.2 Replay Procedure

#### Step 1: Verify Root Cause Resolution
Ensure the upstream service (e.g., CRM or EHR) has recovered from maintenance or outages before triggering replay.

#### Step 2: Trigger Replay via REST API
```bash
curl -X POST "http://localhost:8000/api/v1/referrals/ref_<ID>/replay" \
     -H "Authorization: Bearer <OPERATIONS_ADMIN_JWT>" \
     -H "X-Request-ID: ops-replay-$(date +%s)"
```

#### Step 3: Verify Status
Query the referral to ensure it transitioned to `COMPLETED`:
```bash
curl -X GET "http://localhost:8000/api/v1/referrals/ref_<ID>" \
     -H "Authorization: Bearer <OPERATIONS_ADMIN_JWT>"
```

Expected response:
```json
{
  "referral_id": "ref_<ID>",
  "external_reference": "EXT-...",
  "status": "COMPLETED",
  "hospital": "RIYADH-01",
  "crm_case_id": "CRM-10002",
  "updated_at": "...",
  "request_id": "..."
}
```

---

## 5. Routine Maintenance & Monitoring

### 5.1 Outbox Backlog Monitoring
Monitor `PENDING` publisher backlog and expired worker claims:
```sql
SELECT COUNT(*) AS pending_outbox_backlog
FROM outbox_events
WHERE status = 'PENDING';

SELECT event_id, referral_id, claimed_by, claimed_at
FROM outbox_events
WHERE status = 'PROCESSING'
  AND claimed_at < NOW() - INTERVAL '5 minutes'
ORDER BY claimed_at;
```

Expired `PROCESSING` rows are reclaimed automatically by a healthy worker.
Investigate repeated expiry before manually changing state, because it may
indicate worker crashes or an integration call exceeding the lease duration.

### 5.2 Health Check Endpoints
* **Liveness**: `GET /health/live` returns `200 {"status":"alive"}` while the API process can serve requests.
* **Readiness**: `GET /health/ready` returns `200 {"status":"ready"}` only after a successful PostgreSQL query.
* **Not Ready**: Readiness returns `503 {"status":"not_ready","dependency":"database"}` when PostgreSQL is unavailable.
* **Compatibility Alias**: `GET /health` has the same database-aware behavior as readiness.

Docker uses API readiness for the API service and runs the same PostgreSQL probe
inside the worker container. A running but database-disconnected API or worker
therefore becomes unhealthy instead of reporting a false positive.

### 5.3 Rate-Limit Monitoring

Monitor structured events:

* `rate_limit.exceeded` indicates a client exhausted a named policy.
* `rate_limit.backend_unavailable` means application enforcement failed open.

For `429` responses, verify `X-RateLimit-Policy`, `Retry-After`, request ID, and
the caller's expected traffic pattern. Do not manually delete individual keys
unless an incident commander approves a temporary reset.

If Redis is unavailable, confirm the WAF/API-gateway policy is active, restore
Redis connectivity, and verify application logs stop emitting backend-unavailable
events. Limiter keys expire automatically and contain only hashed identities.
