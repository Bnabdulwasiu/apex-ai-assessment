# Apex AI Arabia — Backend & Integration Architecture

**Document status:** Architecture draft
**Related specification:** `docs/requirements.md`

## 1. Architecture Overview

The prototype implements an asynchronous referral integration backend for a centralized healthcare contact center.

The backend is responsible for:

* authenticating and authorizing API clients;
* accepting and validating referral requests;
* preventing duplicate submissions;
* persisting referral state;
* reliably publishing processing events;
* orchestrating EHR, AI, routing, and CRM integrations;
* handling transient failures and retries;
* moving unrecoverable integration failures to a dead-letter state;
* maintaining an audit trail;
* exposing referral processing status.

The EHR, AI, CRM, Identity Provider, and event queue are represented by replaceable interfaces and local mock implementations.

The prototype intentionally avoids unnecessary distributed infrastructure. The architecture demonstrates the reliability and integration patterns required for production without pretending that the assessment prototype is a production healthcare platform.

---

## 2. High-Level Architecture

```text
                         ┌──────────────────────┐
                         │   Contact Center     │
                         │       Client         │
                         └──────────┬───────────┘
                                    │
                              HTTPS / JWT
                                    │
                                    ▼
                    ┌─────────────────────────────┐
                    │       FastAPI API            │
                    │                             │
                    │ Authentication              │
                    │ Authorization               │
                    │ Validation                  │
                    │ Idempotency                 │
                    │ Referral API                 │
                    └─────────────┬───────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────────┐
                    │         PostgreSQL           │
                    │                             │
                    │ referrals                   │
                    │ idempotency_records         │
                    │ outbox_events                │
                    │ integration_events           │
                    │ audit_logs                   │
                    └─────────────┬───────────────┘
                                  │
                       transactional outbox
                                  │
                                  ▼
                    ┌─────────────────────────────┐
                    │      Outbox Publisher        │
                    └─────────────┬───────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────────┐
                    │       Event Queue            │
                    └─────────────┬───────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────────┐
                    │      Referral Worker         │
                    │                             │
                    │ State / Step Management     │
                    │ Retry Policy                │
                    │ Duplicate Event Protection  │
                    └──────┬──────┬──────┬────────┘
                           │      │      │
                 ┌─────────┘      │      └─────────┐
                 ▼                ▼                ▼
          ┌───────────┐    ┌───────────┐    ┌───────────┐
          │ EHR       │    │ AI        │    │ CRM       │
          │ Adapter   │    │ Adapter   │    │ Adapter   │
          └─────┬─────┘    └─────┬─────┘    └─────┬─────┘
                │                │                │
                ▼                ▼                ▼
           Mock EHR          Mock AI          Mock CRM

                    ┌─────────────────────────────┐
                    │ Identity Provider Adapter   │
                    │ Mock IdP for prototype      │
                    └─────────────────────────────┘
```

---

## 3. Major Components

### 3.1 FastAPI API

The API is the external boundary of the integration service.

Responsibilities:

* authenticate requests;
* authorize access;
* validate request payloads;
* enforce idempotency;
* create referral records;
* create the corresponding outbox event;
* return referral status;
* generate request/correlation identifiers.

The API does **not** synchronously execute the complete referral workflow.

A successful intake returns:

```http
202 Accepted
```

with a referral identifier and current processing status.

---

### 3.2 PostgreSQL

PostgreSQL is the authoritative store for application state.

The prototype uses it for:

* referral records;
* processing state;
* idempotency records;
* transactional outbox events;
* integration attempt information;
* audit records.

The database is also the durability mechanism protecting the system when the queue is temporarily unavailable.

---

### 3.3 Transactional Outbox

Referral creation and creation of the processing event occur in the **same database transaction**.

```text
BEGIN TRANSACTION

    INSERT referral
    INSERT outbox_event

COMMIT
```

This prevents the following failure:

```text
Referral saved
      ↓
Queue publish attempted
      ↓
Queue unavailable
      ↓
Referral exists
BUT
No processing event exists
```

Instead:

```text
Referral saved
      +
Outbox event saved
      ↓
Transaction committed
      ↓
Outbox publisher retries publishing
```

The outbox event remains in PostgreSQL until it has been successfully published.

### Outbox Publisher Retry

The outbox publisher uses a **persistent polling/retry mechanism**, separate from the worker's three-attempt integration retry policy.

If the queue is unavailable:

```text
PENDING
   ↓
publish attempt
   ↓
failure
   ↓
wait/backoff
   ↓
retry
   ↓
...
   ↓
successful publication
   ↓
PUBLISHED
```

The outbox event is not moved to the integration DLQ merely because the queue is temporarily unavailable.

### Worker Claim Lease

Published events are leased to workers rather than immediately marked as
consumed:

```text
PUBLISHED → PROCESSING → CONSUMED
                 │
                 └── expired lease → PROCESSING by a recovery worker
```

The lease is committed before processing starts. Referral state changes and
the final `CONSUMED` transition are committed together. This prevents a crash
after claim from permanently removing the event while still preventing active
workers from processing the same event concurrently.

---

## 4. Event Queue

The queue decouples referral intake from downstream processing.

For the prototype, the queue is represented by a local/mock implementation behind an interface.

The interface can later be replaced by a production messaging system without changing the referral API.

Example event:

```json
{
  "event_id": "evt_001",
  "event_type": "REFERRAL_RECEIVED",
  "referral_id": "ref_001",
  "created_at": "2026-09-24T12:00:00Z"
}
```

Events intentionally contain identifiers and processing metadata rather than complete patient payloads.

---

## 5. Referral Worker

The worker consumes referral events and executes the integration workflow.

```text
REFERRAL_RECEIVED
       │
       ▼
     EHR
       │
       ▼
   Eligibility
       │
       ├── ineligible → ELIGIBILITY_FAILED
       │
       ▼
      AI
       │
       ▼
    Routing
       │
       ▼
      CRM
       │
       ▼
   COMPLETED
```

The worker owns:

* workflow progression;
* integration-step execution;
* retry decisions;
* state transitions;
* failure classification;
* DLQ handling;
* duplicate event protection.

---

## 6. Worker Redelivery Idempotency

Client-level HTTP idempotency and worker-level event idempotency are separate concerns.

`Idempotency-Key` prevents duplicate client submissions.

The worker must additionally tolerate duplicate delivery of the same event.

Before executing an integration step, the worker evaluates the persisted referral state and processing history.

For example:

```text
Event delivered
      ↓
Referral already completed CRM
      ↓
Do not execute CRM again
      ↓
Acknowledge/finish duplicate event
```

Similarly, if EHR and AI have already completed successfully but CRM failed:

```text
EHR       ✓
AI        ✓
Routing   ✓
CRM       ✗
          ↓
Retry event
          ↓
Resume at CRM
```

The workflow therefore does not restart from EHR merely because the queue redelivered an event.

---

## 7. Integration Adapter Layer

External systems are accessed through interfaces rather than directly from workflow code.

```text
Referral Workflow
       │
       ├── EHRAdapter
       ├── AIAdapter
       ├── CRMAdapter
       ├── IdentityProvider
       └── EventPublisher
```

This provides:

* replaceable integrations;
* isolated mock implementations;
* easier testing;
* clearer ownership;
* reusable Apex integration components.

### EHR Adapter

Responsible for:

* eligibility lookup;
* translating internal requests into EHR requests;
* translating EHR responses into internal models.

### AI Adapter

The AI model is supplied by another Apex team.

The backend therefore treats the model as an external dependency.

The adapter isolates:

* authentication;
* request/response contract;
* timeout handling;
* error translation.

AI output is **advisory**.

The final routing decision remains controlled by deterministic backend rules.

### CRM Adapter

Responsible for:

* locating existing CRM cases;
* creating cases;
* updating cases;
* translating CRM failures into internal error categories.

A CRM duplicate is handled through lookup using a stable business reference where possible rather than blindly creating another case.

---

## 8. Referral State Model

The referral lifecycle is represented explicitly.

```text
RECEIVED
   │
   ▼
PROCESSING
   │
   ├──────────────► ELIGIBILITY_FAILED
   │
   ▼
ROUTING
   │
   ▼
CRM_PENDING
   │
   ├──────────────► DEAD_LETTER
   │
   ▼
COMPLETED
```

A more detailed implementation may represent individual processing stages separately while retaining a simple externally visible status.

Important distinction:

### Business failure

Example:

```text
EHR → eligible = false
```

Result:

```text
ELIGIBILITY_FAILED
```

No AI, routing, or CRM processing occurs.

### Technical failure

Example:

```text
CRM → HTTP 503
```

Result:

```text
retry → retry → retry → DEAD_LETTER
```

### Non-retryable technical failure

Example:

```text
CRM → malformed request / 400
```

Result:

```text
DEAD_LETTER
```

No transient retry attempts are consumed.

---

## 9. Retry Architecture

Retries are performed at the **failed integration step**, not against the entire workflow.

Retryable examples:

* timeout;
* connection failure;
* HTTP 429;
* HTTP 500;
* HTTP 502;
* HTTP 503;
* HTTP 504.

Prototype policy:

```text
Attempt 1
   ↓ 500ms
Attempt 2
   ↓ 1s
Attempt 3
   ↓ 2s
Failure
   ↓
DEAD_LETTER
```

For example:

```text
EHR       ✓
AI        ✓
Routing   ✓
CRM       ✗
          ↓
CRM retry
```

EHR and AI are not unnecessarily repeated.

---

## 10. Dead-Letter Handling

When a retryable integration failure exhausts its attempts, the referral enters:

```text
DEAD_LETTER
```

The system records:

* referral ID;
* event ID;
* failed step;
* failure code;
* number of attempts;
* failure timestamp.

The dead-letter record contains no unnecessary sensitive patient information.

Only an `operations_admin` may replay a dead-letter item.

Replay is audited and must continue to use the same duplicate-protection and idempotency mechanisms as normal processing.

---

## 11. Authentication and Authorization

Authentication is performed using a bearer token validated against the mock Identity Provider.

Authorization occurs after authentication.

Prototype roles:

| Role                   | Access                                                                 |
| ---------------------- | ---------------------------------------------------------------------- |
| `contact_center_agent` | Submit referrals and retrieve referrals within assigned hospital scope |
| `integration_service`  | Service-to-service integration operations                              |
| `operations_admin`     | Operational inspection and DLQ recovery                                |

Hospital scope is enforced as an authorization attribute.

Example:

```text
Agent
hospital_scope = RIYADH-01

Request:
GET /referrals/REF-002

Referral:
hospital = RIYADH-02

Result:
403 Forbidden
```

The prototype assumes contact-center agents are assigned to hospital scope. Cross-hospital centralized access can be introduced as a future capability without changing the core integration architecture.

---

## 12. Sensitive Data Boundaries

Patient-like data is treated as sensitive throughout the prototype.

The architecture follows data minimization:

```text
API
 │
 │ patient data where required
 ▼
Referral storage
 │
 │ identifiers + processing metadata
 ▼
Queue
 │
 │ minimal event payload
 ▼
Worker
 │
 ├── EHR
 ├── AI
 └── CRM
```

Logs should contain:

* request ID;
* referral ID;
* event ID;
* processing stage;
* outcome;
* error code;
* retry count.

Logs should not contain unnecessary patient details or complete clinical payloads.

Application processes write JSON Lines to standard output. Every record has a
stable timestamp, level, logger, service, event, and message schema. The API and
worker emit `service.started` events containing release and runtime identity;
the worker additionally reports its bounded concurrency, polling, lease, batch,
and retry configuration. Connection strings, credentials, signing secrets, and
referral payloads are excluded from startup metadata.

Audit records similarly record **what happened** without becoming a copy of the clinical payload.

---

## 13. Trust Boundaries

The major trust boundaries are:

```text
                    TRUST BOUNDARY
                          │
Contact Center ───► FastAPI API
                          │
                          ▼
                    Internal System
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
             EHR         AI          CRM
          External      External    External
          Adapter       Adapter     Adapter
```

Authentication and authorization are enforced at the API boundary.

External integrations are isolated behind adapters so that external contracts and failures do not leak directly into the internal domain model.

### Distributed Rate-Limit Boundary

Authenticated API operations pass through a Redis-backed token bucket before
business processing. Every API replica executes the same atomic Lua operation,
using Redis server time to avoid process clock drift and double-spending under
concurrency. Keys use a one-way hash of the client ID plus a policy name; no
patient data, bearer token, or referral content is stored in Redis.

Failed authentication attempts use the direct network peer address. A reverse
proxy must sanitize forwarded-address headers before the application is ever
configured to trust them. Health probes bypass rate limiting.

Redis is an availability control rather than a clinical system of record.
Limiter state is intentionally ephemeral. If Redis is unavailable, requests
fail open and a structured `rate_limit.backend_unavailable` event is emitted;
the production WAF/API gateway supplies the independent outer limit.

---

## 14. Failure Handling Summary

| Failure                       | Handling                     |
| ----------------------------- | ---------------------------- |
| Invalid request               | Reject with 4xx              |
| Unauthenticated request       | 401                          |
| Unauthorized resource         | 403 + audit                  |
| Duplicate HTTP request        | Return existing referral     |
| EHR says ineligible           | `ELIGIBILITY_FAILED`         |
| EHR timeout                   | Retry EHR                    |
| AI timeout                    | Retry AI                     |
| CRM 503                       | Retry CRM                    |
| CRM 400                       | `DEAD_LETTER`                |
| Retryable failure exhausted   | `DEAD_LETTER`                |
| Queue temporarily unavailable | Outbox remains pending       |
| Duplicate event delivery      | Worker skips completed steps |
| Unauthorized DLQ replay       | Reject + audit               |
| API limit exceeded            | 429 + `Retry-After`           |
| Redis limiter unavailable     | Allow + structured alert      |

---

## 15. Scalability Path

The initial workload is approximately:

```text
10,000 referral/appointment events/day
```

with a future target of approximately:

```text
100,000 events/day
```

The architecture supports this progression through:

* stateless API instances;
* asynchronous processing;
* horizontally scalable workers;
* queue-based buffering;
* PostgreSQL indexing;
* controlled downstream concurrency;
* replaceable integration adapters.

Each worker process uses a bounded consumer pool controlled by
`WORKER_CONCURRENCY` (default 4, valid range 1-8). A consumer handles one
referral at a time, so this value is also the maximum number of concurrent
calls to any one downstream integration from that process. PostgreSQL
`FOR UPDATE SKIP LOCKED` leasing prevents consumer slots and horizontally
scaled worker processes from claiming the same outbox event.

The prototype does not introduce Kubernetes, Kafka, or a microservice fleet solely to demonstrate scale. Production infrastructure choices can be made based on measured throughput, reliability requirements, and operational constraints.

---

## 16. Reusable Apex Components

The architecture intentionally creates reusable components:

```text
Authentication / Authorization
Idempotency Middleware
Transactional Outbox
Event Publisher
Retry Policy
Integration Adapter Interface
Audit Logger
Structured Error Model
DLQ / Replay Mechanism
Correlation ID Handling
Distributed Rate Limiter
```

These components can later support additional healthcare workflows such as appointment, claims, or other referral-related integrations.

---

## 17. End-to-End Success Flow

```text
Client
  │
  │ POST /api/v1/referrals
  ▼
Authenticate
  │
  ▼
Authorize
  │
  ▼
Validate
  │
  ▼
Check Idempotency
  │
  ▼
DB Transaction
 ├── Referral
 └── Outbox Event
  │
  ▼
202 Accepted
  │
  ▼
Outbox Publisher
  │
  ▼
Event Queue
  │
  ▼
Worker
  │
  ▼
EHR Eligibility
  │
  ▼
AI Classification
  │
  ▼
Deterministic Routing
  │
  ▼
CRM Lookup/Create/Update
  │
  ▼
Audit + State Update
  │
  ▼
COMPLETED
```

---

## 18. End-to-End Failure Flow

```text
Worker
  │
  ▼
CRM
  │
  ├── 503
  │
  ▼
Retry #1
  │
  ├── 503
  │
  ▼
Retry #2
  │
  ├── 503
  │
  ▼
Retry #3
  │
  ├── 503
  │
  ▼
DEAD_LETTER
  │
  ▼
Operations Admin
  │
  ▼
Investigate
  │
  ▼
Authorized Replay
  │
  ▼
Resume safely using idempotency
```

---

## 19. Architectural Principles

The implementation should preserve these principles:

1. **Persist before publishing.**
2. **Never rely on event delivery exactly once.**
3. **Make processing idempotent.**
4. **Retry transient failures, not everything.**
5. **Retry the failed step, not the entire workflow.**
6. **Separate business failures from technical failures.**
7. **Keep external systems behind adapters.**
8. **Minimize sensitive data exposure.**
9. **Make operational recovery explicit and authorized.**
10. **Prefer simple, demonstrable architecture over unnecessary infrastructure.**
