# Apex AI Arabia — Healthcare Referral Integration Backend

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![SQLAlchemy Async](https://img.shields.io/badge/SQLAlchemy-2.0%20Async-red.svg)](https://www.sqlalchemy.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791.svg)](https://www.postgresql.org/)
[![Test Coverage](https://img.shields.io/badge/Coverage-82%25-brightgreen.svg)]()
[![Tests](https://img.shields.io/badge/Tests-62%20Passing-success.svg)]()

Production-grade backend prototype for AI-assisted referral intake, eligibility verification, triage classification, facility routing, and CRM synchronization for a Saudi private healthcare group.

Built to withstand real enterprise evaluation and aligned with Apex AI Arabia's **PROVE $\rightarrow$ REPEAT $\rightarrow$ PRODUCTIZE $\rightarrow$ SCALE** doctrine.

---

## 1. Architectural Doctrine Alignment

```
   PROVE               REPEAT             PRODUCTIZE             SCALE
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Validate     │    │ Standardize  │    │ Hardened     │    │ Enterprise   │
│ End-to-End   │───►│ Patterns &   │───►│ Security,    │───►│ Multi-Region │
│ Workflow     │    │ Adapters     │    │ DLQ & Outbox │    │ & High Avail │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

* **PROVE**: Demonstrates the full referral journey from intake to CRM case creation with real asynchronous workflow orchestration.
* **REPEAT**: Encapsulates external systems behind clean, reusable **Ports & Adapters** (`EHRAdapter`, `AIAdapter`, `CRMAdapter`, `EventQueue`).
* **PRODUCTIZE**: Hardened with **Transactional Outbox**, **Hash-Based Idempotency**, **Redis Token-Bucket Rate Limiting**, **Step-Level Resumption**, **3-Attempt Exponential Backoff**, **Dead Letter Queue (DLQ)**, and **Role-Gated Replay**.
* **SCALE**: Stateless API layer, connection pooling, isolated background workers, and PDPL-aligned privacy controls. Formal production compliance remains an organizational responsibility.

---

## 2. System Architecture

```
                  ┌────────────────────────────────────────────────────────┐
                  │                    EXTERNAL CLIENTS                    │
                  │   (Contact Center Agents, Ops Admins, Integrations)    │
                  └───────────────────────────┬────────────────────────────┘
                                              │ HTTPS / JWT Bearer
                                              ▼
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ FASTAPI PRESENTATION LAYER (src/api/)                                                    │
│  ├── Multi-Hospital Scoped RBAC (src/auth/)                                             │
│  ├── POST /api/v1/referrals (202 Accepted)                                               │
│  ├── GET  /api/v1/referrals/{id} (Non-Enumerating 404 & Patient Data Masking)            │
│  └── POST /api/v1/referrals/{id}/replay (Authorized Operations DLQ Replay)               │
└─────────────────────────────────────────────┬────────────────────────────────────────────┘
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      ▼                                               ▼
┌──────────────────────────────────────────┐    ┌──────────────────────────────────────────┐
│ REFERRAL SERVICE (src/services/)         │    │ REPOSITORIES (src/repositories/)         │
│  ├── Hash-Based Idempotency Check        │◄───┤  ├── IdempotencyRepository               │
│  └── Atomic Multi-Table Flush            │    │  ├── ReferralRepository                  │
└─────────────────────┬────────────────────┘    │  ├── OutboxRepository                    │
                      │                         │  ├── IntegrationRepository               │
                      │ Same Transaction        │  └── AuditRepository                     │
                      ▼                         └──────────────────────────────────────────┘
┌──────────────────────────────────────────┐
│ POSTGRESQL 16 PERSISTENCE LAYER          │
│  ├── referrals (Core business entity)    │
│  ├── idempotency_records (SHA-256 key)   │
│  ├── outbox_events (Durable Queue Buffer)│
│  ├── integration_events (Step Telemetry) │
│  └── audit_logs (Immutable Audit Trail)  │
└─────────────────────┬────────────────────┘
                      │ Poll PENDING
                      ▼
┌──────────────────────────────────────────┐
│ OUTBOX PUBLISHER (src/services/)         │
│  └── At-Least-Once Delivery to Queue     │
└─────────────────────┬────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────┐
│ ASYNC WORKER PIPELINE (src/services/)    │
│  └── Step-level Idempotency & Retries    │
│       1. EHR Check  ───► [EHRAdapter]    │
│       2. AI Triage  ───► [AIAdapter]     │
│       3. Routing    ───► [RoutingService]│
│       4. CRM Sync   ───► [CRMAdapter]    │
└──────────────────────────────────────────┘
```

---

## 3. Quick Start Guide

### Prerequisites
* Python 3.12+ (managed with `uv`)
* Docker & Docker Compose (for PostgreSQL and Redis)

### 3.1 Clone & Install Dependencies
```bash
uv sync --all-extras
```

Copy the environment template and replace both `replace_with...` values with
long random secrets. Keep `POSTGRES_PASSWORD` identical to the password inside
`DATABASE_URL` and `DOCKER_DATABASE_URL`.

```bash
# Linux / macOS
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

### 3.2 Start Infrastructure & Run Migrations
```bash
# Start PostgreSQL 16 and Redis 7.4
docker compose up -d postgres redis

# Run Alembic schema migrations
uv run alembic upgrade head
```

### 3.3 Run the Complete Stack with Docker
Build and start PostgreSQL, migrations, the API, and the worker:

```bash
docker compose up --build
```

The `migrate` container exiting successfully is expected; the `api`, `worker`,
`postgres`, and `redis` services remain running. Open:

* **Swagger Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)
* **Readiness Check**: [http://localhost:8000/health/ready](http://localhost:8000/health/ready)
* **Liveness Check**: [http://localhost:8000/health/live](http://localhost:8000/health/live)

Useful operational commands:

```bash
docker compose ps
docker compose logs -f migrate api worker redis
docker compose down
```

### 3.4 Run the Interactive Live Demonstration
Execute the full 8-step enterprise lifecycle in your console:
```bash
uv run python scripts/demo.py
```

### 3.5 Start the FastAPI Server Without Docker
```bash
uv run uvicorn src.main:app --reload
```
* **Interactive Swagger Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)
* **Readiness Check**: [http://localhost:8000/health/ready](http://localhost:8000/health/ready)

### 3.6 Start the Worker Without Docker
Run the durable outbox publisher and referral processor in a separate process:
```bash
uv run python -m src.worker_main
```

Each worker process starts four consumer slots by default. Set
`WORKER_CONCURRENCY` from 1 to 8 to bound simultaneous downstream integration
calls per process. PostgreSQL `SKIP LOCKED` leasing assigns distinct events to
the slots; additional worker processes can be added for horizontal scaling.

On `Ctrl+C` or `SIGTERM`, the worker stops claiming new events, finishes its
current referral, and then closes its database connections. Uvicorn likewise
drains active API requests before the FastAPI lifespan closes the API database
pool. The deployment platform should provide the outer forced-termination
timeout if a downstream integration never returns.

---

## 4. Running the Automated Test Suite

The test suite runs against an isolated async in-memory database and verifies all API contracts, security rules, and failure scenarios.

```bash
# Run all 62 tests
uv run pytest -v

# Run with full code coverage report
uv run pytest --cov=src --cov-report=term-missing
```

### Test Coverage Breakdown
```text
============================= 62 passed in 8.58s ==============================
TOTAL COVERAGE: 82% across 1,366 statements

Test Suite Distribution:
├── API contracts, authentication, RBAC, idempotency, and distributed rate limits
├── EHR, AI, CRM, routing, queue, outbox, and replay behavior
└── Worker lifecycle, bounded concurrency, and structured logging
```

---

## 5. Key Design Decisions & Defensibility

### 5.1 Transactional Outbox Pattern
* **Problem**: Network partitions between the API server and message broker risk losing referrals after database commitment.
* **Solution**: The referral, idempotency record, outbox event, and audit log are written in the **exact same database flush**. The `OutboxPublisher` polls `PENDING` outbox records and marks them `PUBLISHED` only after successful delivery confirmation.

### 5.2 Multi-Hospital Security & Non-Enumerating 404s
* **Problem**: Attackers probing referral IDs across hospitals could enumerate patient records at other facilities.
* **Solution**: Contact center agents are scoped to their authorized hospital (e.g. `RIYADH-01`). If an agent attempts to access a `RIYADH-02` referral, the API returns **404 Not Found** rather than 403 Forbidden, preventing resource enumeration.

### 5.3 Step-Level Idempotency & Resumption
* **Problem**: Replaying a referral from the Dead Letter Queue shouldn't repeat already-completed external calls (e.g. re-running EHR or AI triage).
* **Solution**: The worker checks `integration_events` before every step. If EHR and AI previously completed successfully, the worker skips them and resumes execution directly at the failed CRM step.

### 5.4 Business Rejection vs. Technical Failure
* **Business Outcome**: An ineligible patient response from the EHR transitions cleanly to `ELIGIBILITY_FAILED` and halts processing.
* **Technical Failure**: Upstream 503s or timeouts trigger 3 exponential backoff retries (`500ms → 1s → 2s`) before moving to `DEAD_LETTER`.

### 5.5 Structured Operational Logging
* **Format**: Application logs are emitted as one JSON object per line for direct ingestion by container and cloud logging platforms.
* **Startup Identity**: API and worker startup events include service, release version, environment, host, process ID, and safe operational settings.
* **Data Boundary**: Secrets and patient-sensitive field names are redacted; startup events never include connection strings, signing keys, or referral payloads.

### 5.6 Distributed Rate Limiting
* **Algorithm**: Redis-backed token buckets use Redis server time and one atomic Lua operation, so horizontally scaled API instances share the same decision state.
* **Identity Boundary**: Authenticated calls are limited by hashed client identity and policy name. Failed authentication attempts use the direct peer IP; JWTs and patient identifiers never enter limiter keys.
* **Policies**: Intake allows 30 requests/minute with burst 10, status reads 120/minute with burst 30, replay 5/minute with burst 2, and failed authentication 10/minute with burst 10.
* **Failure Policy**: If Redis is unavailable, application enforcement fails open and emits a structured operational error. A production WAF/API gateway remains the outer abuse-control layer.

---

## 6. Repository Layout

```text
Apex_Backend_Assessment/
├── alembic/                      # Database migration scripts
│   └── versions/0001_initial.py  # Complete DDL migration
├── docs/                         # Architectural and operational documentation
│   ├── api-contract.md           # Formal API specifications
│   ├── architecture.md           # Architecture and state machine design
│   ├── requirements.md           # Functional & non-functional requirements
│   ├── assumptions.md            # Explicit architectural assumptions (A-01..A-12)
│   ├── runbook.md                # Incident response & DLQ triage procedures
│   └── deployment.md             # Production deployment & Saudi PDPL compliance
├── scripts/
│   └── demo.py                   # Complete interactive CLI walkthrough
├── src/
│   ├── adapters/                 # Ports & Adapters (EHR, AI, CRM, Queue)
│   │   ├── base.py               # Abstract interfaces & DTOs
│   │   └── mocks.py              # Configurable mocks with fault injection
│   ├── api/                      # Presentation layer
│   │   ├── dependencies.py       # Auth & correlation ID dependencies
│   │   └── routes.py             # Versioned REST routes (/api/v1)
│   ├── auth/                     # JWT tokens, RBAC & hospital scoping
│   ├── models/                   # SQLAlchemy domain entities
│   │   ├── referral.py           # Core referral entity & state machine
│   │   ├── idempotency.py        # SHA-256 idempotency records
│   │   ├── outbox.py             # Transactional outbox events
│   │   ├── integration.py        # Step-level attempt telemetry
│   │   └── audit.py              # Immutable audit trail
│   ├── repositories/             # Lightweight data access layer
│   ├── schemas/                  # Pydantic serialization schemas
│   ├── services/                 # Business logic & workflow orchestration
│   │   ├── rate_limiter.py       # Atomic Redis token-bucket adapter
│   │   ├── referral_service.py   # Intake with hash-based idempotency
│   │   ├── worker.py             # Background pipeline orchestrator
│   │   ├── routing.py            # Deterministic facility capability engine
│   │   ├── retry.py              # Exponential backoff classifier
│   │   └── outbox_publisher.py   # Durable outbox publisher
│   ├── config.py                 # Pydantic settings management
│   ├── database.py               # Async engine & session management
│   ├── logging_config.py         # Shared JSON logging and startup metadata
│   ├── rate_limit.py             # Redis client lifecycle and dependency
│   └── main.py                   # FastAPI application factory
├── tests/                        # Comprehensive automated test suite
├── Dockerfile                    # Pinned, non-root production image
├── docker-compose.yml            # Local acceptance stack
└── pyproject.toml                # Dependencies & build configuration
```

---

## 7. AI Disclosure Statement

In accordance with enterprise assessment transparency guidelines:
* **Tooling Used**: Antigravity AI Pair-Programming Assistant (Google DeepMind) and OpenAI ChatGPT/Codex.
* **Scope of Assistance**: Project scaffolding, architectural alignment against provided specifications, test suite construction, and documentation drafting.
* **Human Engineering Review**: All architectural design decisions, state transitions, security boundary enforcements, idempotency hashing algorithms, and error classifications were verified for correctness and enterprise defensibility.
