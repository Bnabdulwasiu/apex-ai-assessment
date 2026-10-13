# Apex AI Arabia — API & Integration Contract

**Document status:** Implementation contract  
**API version:** `v1`  
**Base path:** `/api/v1`

## 1. Contract Purpose

This document defines the externally visible API and the internal integration contracts required by the referral-processing prototype.

The contract covers:

- referral intake;
- referral status retrieval;
- authentication and authorization expectations;
- idempotency;
- error responses;
- referral lifecycle states;
- EHR integration;
- AI integration;
- routing;
- CRM integration;
- event/outbox messages.

The prototype uses mock implementations for external systems, but their interfaces should resemble credible production integrations.

---

# 2. API Conventions

### Base URL

```text
/api/v1
```

### Content Type

```http
Content-Type: application/json
```

### Authentication

```http
Authorization: Bearer <access-token>
```

### Correlation

Clients may provide:

```http
X-Request-ID: <request-id>
```

If omitted, the API generates a request ID.

The request ID must be returned in the response and included in structured logs.

### Idempotency

Referral creation requires:

```http
Idempotency-Key: <unique-client-key>
```

The key is scoped to the authenticated client:

```text
(client_id, idempotency_key)
```

---

# 3. POST `/referrals`

Creates a new referral for asynchronous processing.

## Request

```http
POST /api/v1/referrals
Authorization: Bearer <access-token>
Content-Type: application/json
Idempotency-Key: req-20260924-000001
X-Request-ID: request-abc123
```

### Request body

```json
{
  "external_reference": "CONTACT-REF-10001",
  "patient": {
    "id": "PAT-SYN-001"
  },
  "specialty": "cardiology",
  "reason": "Routine cardiology referral",
  "preferred_hospital": "RIYADH-01"
}
```

### Field definitions

| Field | Type | Required | Classification | Description |
|---|---|---:|---|---|
| `external_reference` | string | Yes | Internal/business | Client-supplied business reference |
| `patient.id` | string | Yes | Sensitive | Synthetic patient identifier |
| `specialty` | string | Yes | Operational | Requested specialty |
| `reason` | string | Yes | Sensitive | Referral reason |
| `preferred_hospital` | string | Yes | Operational | Preferred hospital |

`external_reference` is a client business reference and is distinct from the internally generated `referral_id`.

---

# 4. Successful Intake Response

The API does not wait for EHR, AI, routing, or CRM processing.

It returns:

```http
HTTP/1.1 202 Accepted
```

Example:

```json
{
  "referral_id": "ref_01J8ABC123",
  "status": "RECEIVED",
  "crm_case_id": null,
  "request_id": "request-abc123"
}
```

The client can subsequently retrieve the processing status.

---

# 5. GET `/referrals/{referral_id}`

Retrieves the current state of a referral.

```http
GET /api/v1/referrals/ref_01J8ABC123
Authorization: Bearer <access-token>
X-Request-ID: request-def456
```

### Successful response

```http
HTTP/1.1 200 OK
```

```json
{
  "referral_id": "ref_01J8ABC123",
  "external_reference": "CONTACT-REF-10001",
  "status": "COMPLETED",
  "hospital": "RIYADH-01",
  "crm_case_id": "CRM-10001",
  "updated_at": "2026-09-24T12:04:30Z",
  "request_id": "request-def456"
}
```

The API does not expose the complete clinical/referral payload through this status endpoint.

---

# 6. Referral Statuses

The externally visible status values are:

```text
RECEIVED
PROCESSING
ELIGIBILITY_FAILED
ROUTED
CRM_PENDING
COMPLETED
DEAD_LETTER
```

### Meaning

| Status | Meaning |
|---|---|
| `RECEIVED` | Request accepted and persisted |
| `PROCESSING` | Background workflow is executing |
| `ELIGIBILITY_FAILED` | EHR determined the referral is not eligible |
| `ROUTED` | Routing decision completed |
| `CRM_PENDING` | CRM processing remains outstanding |
| `COMPLETED` | Referral successfully processed |
| `DEAD_LETTER` | Processing stopped after an unrecoverable integration failure |

Internal processing-stage information may be more detailed than the externally exposed status.

---

# 7. Idempotency Contract

The client must provide an `Idempotency-Key`.

## Same key + same request

The API returns the previously created referral.

Example:

```text
First request
    ↓
Idempotency-Key = abc
    ↓
referral = ref_001

Second identical request
    ↓
Idempotency-Key = abc
    ↓
return ref_001
```

No second workflow is created.

---

## Same key + different request

Example:

```text
Idempotency-Key: abc

Request A:
patient.id = PAT-SYN-001

Request B:
patient.id = PAT-SYN-002
```

Response:

```http
409 Conflict
```

```json
{
  "error": {
    "code": "IDEMPOTENCY_KEY_REUSED",
    "message": "The idempotency key has already been used with a different request.",
    "request_id": "request-xyz789",
    "retryable": false
  }
}
```

---

## Same key while processing

The existing referral is returned rather than starting another workflow.

```json
{
  "referral_id": "ref_01J8ABC123",
  "status": "PROCESSING",
  "crm_case_id": null,
  "request_id": "request-xyz789"
}
```

---

# 8. Authentication Errors

## Missing or invalid token

```http
401 Unauthorized
```

```json
{
  "error": {
    "code": "UNAUTHENTICATED",
    "message": "Authentication is required.",
    "request_id": "request-123",
    "retryable": false
  }
}
```

Authentication failures should be recorded as security-relevant audit events where appropriate.

---

# 9. Authorization Errors

A valid agent attempting to access a referral outside their hospital scope receives:

```http
403 Forbidden
```

```json
{
  "error": {
    "code": "FORBIDDEN",
    "message": "You are not authorized to access this resource.",
    "request_id": "request-124",
    "retryable": false
  }
}
```

The system should not reveal whether an inaccessible resource exists.

The failed authorization attempt is audited.

---

# 10. Validation Errors

Invalid input returns:

```http
422 Unprocessable Entity
```

Example:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed.",
    "request_id": "request-125",
    "retryable": false,
    "details": [
      {
        "field": "specialty",
        "reason": "Field is required."
      }
    ]
  }
}
```

Validation failures do not create processing work.

---

# 11. Not Found

A referral that is accessible to the requesting principal but does not exist returns:

```http
404 Not Found
```

```json
{
  "error": {
    "code": "REFERRAL_NOT_FOUND",
    "message": "Referral was not found.",
    "request_id": "request-126",
    "retryable": false
  }
}
```

---

# 12. Error Model

All application errors follow the same basic structure:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable description.",
    "request_id": "request-123",
    "retryable": false
  }
}
```

Optional:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed.",
    "request_id": "request-123",
    "retryable": false,
    "details": []
  }
}
```

Errors must not expose:

- stack traces;
- credentials;
- tokens;
- database details;
- internal infrastructure details;
- unnecessary patient information.

---

# 13. EHR Adapter Contract

The EHR integration is represented by an adapter.

Conceptually:

```python
class EHRAdapter:
    async def check_eligibility(
        self,
        referral: ReferralContext
    ) -> EligibilityResult:
        ...
```

### Request

```json
{
  "patient_id": "PAT-SYN-001",
  "specialty": "cardiology",
  "hospital": "RIYADH-01"
}
```

### Eligible response

```json
{
  "eligible": true,
  "eligibility_reference": "ELIG-001"
}
```

### Ineligible response

```json
{
  "eligible": false,
  "reason_code": "INSURANCE_NOT_ELIGIBLE"
}
```

The latter is a **business outcome**, not a technical failure.

The referral transitions to:

```text
ELIGIBILITY_FAILED
```

and processing stops.

### Technical failure examples

```text
timeout
connection failure
429
500
502
503
504
```

These are retryable according to the worker retry policy.

---

# 14. AI Adapter Contract

The AI service is supplied by another Apex team.

The backend interacts with it through an adapter.

Conceptually:

```python
class AIAdapter:
    async def classify(
        self,
        referral: ReferralContext
    ) -> AIClassification:
        ...
```

### Example request

```json
{
  "referral_id": "ref_01J8ABC123",
  "specialty": "cardiology",
  "reason": "Routine cardiology referral"
}
```

### Example response

```json
{
  "classification": "CARDIOLOGY",
  "urgency": "HIGH",
  "confidence": 0.94
}
```

These fields are prototype contract choices and are not claimed to be requirements from the assessment.

The AI result is **advisory**.

It is passed to deterministic routing logic rather than directly determining the final destination.

---

# 15. Routing Contract

Routing is an internal deterministic component.

Conceptually:

```python
class RoutingService:
    def route(
        self,
        referral: ReferralContext,
        ai_result: AIClassification
    ) -> RoutingDecision:
        ...
```

### Example result

```json
{
  "hospital": "RIYADH-01",
  "specialty": "CARDIOLOGY",
  "routing_reason": "SPECIALTY_AND_HOSPITAL_RULE"
}
```

The prototype should make routing rules explicit and configurable rather than embedding them throughout the worker.

---

# 16. CRM Adapter Contract

Conceptually:

```python
class CRMAdapter:
    async def find_case(
        self,
        external_reference: str
    ) -> CRMCase | None:
        ...

    async def create_case(
        self,
        referral: ReferralContext
    ) -> CRMCase:
        ...

    async def update_case(
        self,
        case_id: str,
        update: CRMUpdate
    ) -> CRMCase:
        ...
```

### CRM case example

```json
{
  "case_id": "CRM-10001",
  "external_reference": "CONTACT-REF-10001",
  "status": "OPEN"
}
```

### Duplicate case handling

Before creating a case, the integration should attempt to identify an existing case using the stable external business reference.

This prevents:

```text
retry
  ↓
blind CRM create
  ↓
duplicate case
```

from occurring after an uncertain previous attempt.

---

# 17. Event / Outbox Contract

The initial processing event:

```json
{
  "event_id": "evt_01J8XYZ",
  "event_type": "REFERRAL_RECEIVED",
  "referral_id": "ref_01J8ABC123",
  "created_at": "2026-09-24T12:00:00Z"
}
```

Additional metadata may include:

```json
{
  "attempt": 1,
  "correlation_id": "request-abc123"
}
```

Events should contain the minimum information required to process the workflow.

They should not contain unnecessary patient or clinical information.

---

# 18. Audit Event Contract

Audit records follow a consistent structure:

```json
{
  "timestamp": "2026-09-24T12:01:00Z",
  "actor": "contact_center_agent",
  "action": "REFERRAL_CREATED",
  "resource_type": "referral",
  "resource_id": "ref_01J8ABC123",
  "outcome": "SUCCESS",
  "request_id": "request-abc123"
}
```

Examples of auditable events:

```text
REFERRAL_CREATED
REFERRAL_STATE_CHANGED
EHR_CHECK_COMPLETED
AI_CLASSIFICATION_COMPLETED
CRM_CASE_CREATED
CRM_CASE_UPDATED
RETRY_ATTEMPTED
DEAD_LETTER_CREATED
DEAD_LETTER_REPLAYED
AUTHENTICATION_FAILED
AUTHORIZATION_FAILED
```

Audit records should not contain complete patient payloads.

---

# 19. Retry Contract

Worker integration retries use:

```text
Maximum attempts: 3
Backoff: 500ms → 1s → 2s
```

Retryable:

```text
timeout
connection failure
429
500
502
503
504
```

Non-retryable:

```text
400
401
403
404
malformed request
business rejection
```

A non-retryable technical failure moves directly to:

```text
DEAD_LETTER
```

without consuming transient retry attempts.

Retries occur at the failed step.

---

# 20. Outbox Publisher Contract

The outbox publisher has a separate reliability model.

It does **not** use the worker's three-attempt integration policy.

An unpublished outbox event remains durable in PostgreSQL:

```text
outbox_events.status = PENDING
```

The publisher repeatedly attempts delivery using persistent polling and backoff.

Once the queue confirms successful publication:

```text
PENDING → PUBLISHED
```

A temporary queue outage therefore does not permanently lose the processing event.

---

# 21. Worker Event Redelivery

The event system is treated as **at-least-once delivery**.

The worker must therefore safely handle duplicate events.

The worker uses persisted referral state and processing information to determine which steps have already completed.

Example:

```text
Event #1
EHR ✓
AI ✓
Routing ✓
CRM ✓
COMPLETED

Event #1 redelivered

Worker:
Referral already COMPLETED
       ↓
Do not repeat integration work
```

This mechanism is independent of HTTP `Idempotency-Key`.

---

# 22. API Versioning

The initial API is:

```text
/api/v1
```

Backward-compatible additions may be introduced within the version.

Breaking contract changes require a new API version.

Example:

```text
/api/v2/referrals
```

The prototype does not implement multiple API versions unless required for demonstration.

---

# 23. Data Classification

| Data | Classification | Handling |
|---|---|---|
| Patient ID | Sensitive | Minimize exposure |
| Referral reason | Sensitive | Do not log unnecessarily |
| External reference | Internal/business | Log only when operationally necessary |
| Referral ID | Operational | Safe for correlation |
| Request ID | Operational | Safe for correlation |
| Event ID | Operational | Safe for correlation |
| Hospital ID | Operational | Access-controlled |
| Error code | Operational | Safe for logs |
| Access token | Secret | Never log |
| Credentials | Secret | Never commit/log |

All patient data used by the prototype must be synthetic.

---

# 24. Contract Design Principle

The API contract deliberately separates:

```text
External API
      ↓
Internal domain model
      ↓
Integration adapters
      ↓
External system contracts
```

External EHR, AI, and CRM payloads should not become the application's internal data model.

This keeps the integration layer replaceable and makes the backend reusable across additional workflows.

---

# 25. Required Contract Tests

The implementation should demonstrate at minimum:

### API

- valid referral accepted;
- invalid referral rejected;
- unauthenticated request rejected;
- unauthorized hospital access rejected;
- referral status retrieval;
- unknown referral handling.

### Idempotency

- same key + same payload;
- same key + different payload;
- concurrent duplicate submissions.

### Integration

- EHR eligible;
- EHR ineligible;
- EHR timeout;
- AI timeout;
- CRM success;
- CRM timeout;
- CRM duplicate;
- non-retryable CRM failure.

### Reliability

- retry behavior;
- retry exhaustion;
- DLQ creation;
- authorized DLQ replay;
- unauthorized DLQ replay;
- queue unavailable with pending outbox;
- duplicate event delivery.

### Security

- invalid token;
- missing token;
- cross-hospital access;
- excessive role access;
- sensitive information absent from logs.