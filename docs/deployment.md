# Production Deployment & Infrastructure Guide

## 1. Overview
This document outlines production deployment strategies, infrastructure architecture, containerization, and data protection controls for the **Apex AI Arabia Healthcare Referral Backend**.

---

## 2. Target Production Architecture

```
                       ┌────────────────────────────────┐
                       │        AWS / Oracle Cloud      │
                       │    (Saudi Arabia Region - KSA) │
                       └───────────────┬────────────────┘
                                       │ HTTPS (TLS 1.3)
                                       ▼
                       ┌────────────────────────────────┐
                       │    Application Load Balancer   │
                       │     (WAF + SSL Termination)    │
                       └───────────────┬────────────────┘
                                       │
                ┌──────────────────────┼──────────────────────┐
                ▼                      ▼                      ▼
┌───────────────────────────────┐             ┌───────────────────────────────┐
│     FastAPI API Pods (2+)     │             │    Async Worker Pods (2+)     │
│  - Stateless REST endpoints   │             │  - Outbox Publisher Loop      │
│  - Intake & Hash Idempotency  │             │  - Referral Workflow Engine   │
│  - JWT & Hospital Scope RBAC  │             │  - Exponential Retry & DLQ    │
└───────────────┬───────────────┘             └───────────────┬───────────────┘
                │
                ▼
       ┌───────────────────┐
       │ Managed Redis     │
       │ Shared API Limits │
       └───────────────────┘
                │                                             │
                └──────────────────────┬──────────────────────┘
                                       │
                                       ▼
                       ┌────────────────────────────────┐
                       │    Amazon RDS / Managed PG 16  │
                       │   (Multi-AZ, Primary + Replica)│
                       │   - KMS Encrypted Storage (AES)│
                       │   - Transactional Outbox Table │
                       └────────────────────────────────┘
```

---

## 3. Configuration & Environment Variables

| Variable | Description | Example (Production) | Classification |
| :--- | :--- | :--- | :--- |
| `DATABASE_URL` | Async PostgreSQL connection string | `postgresql+asyncpg://app_user:pwd@rds-endpoint:5432/apex_prod` | Secret |
| `APP_ENV` | Environment identifier | `production` | Operational |
| `APP_VERSION` | Deployed application version included in startup logs | Release identifier | Operational |
| `LOG_LEVEL` | Application logging verbosity | `INFO` | Operational |
| `JWT_SECRET_KEY` | HMAC or Asymmetric Public Key (JWKS) | Vault / AWS Secrets Manager reference | Secret |
| `JWT_ALGORITHM` | JWT Signature Algorithm | `RS256` or `HS256` | Operational |
| `OUTBOX_POLL_INTERVAL_SECONDS` | Outbox publisher polling cadence | `2` | Operational |
| `WORKER_POLL_INTERVAL_SECONDS` | Worker queue polling cadence | `2` | Operational |
| `WORKER_CONCURRENCY` | Concurrent referral consumer slots per worker process (1-8) | `4` | Operational |
| `OUTBOX_CLAIM_LEASE_SECONDS` | Worker claim recovery timeout | `300` | Operational |
| `RETRY_MAX_ATTEMPTS` | Maximum integration retry attempts | `3` | Operational |
| `RETRY_BACKOFF_BASE_MS` | Initial retry backoff interval | `500` | Operational |
| `REDIS_URL` | Shared Redis connection for API rate limiting | Managed Redis endpoint | Secret/Connection |
| `RATE_LIMIT_ENABLED` | Enable application-level enforcement | `true` | Operational |
| `RATE_LIMIT_BACKEND_TIMEOUT_SECONDS` | Maximum Redis connect/operation wait | `0.25` | Operational |
| `RATE_LIMIT_*_PER_MINUTE` | Sustained limits by operation | See `.env.example` | Operational |
| `RATE_LIMIT_*_BURST` | Token-bucket burst capacities | See `.env.example` | Operational |

---

## 4. Saudi PDPL-Aligned Healthcare Controls

The prototype demonstrates controls that support a future **Saudi Personal Data
Protection Law (PDPL)** deployment. It is not a compliance certification;
production deployment requires legal, security, privacy, and infrastructure
review. The target deployment should provide:

1. **In-Kingdom Data Residency**:
   - Production PostgreSQL databases and container compute nodes must be provisioned strictly within Saudi Arabia data regions (e.g. `me-central-1` / `me-south-1`).
2. **Encryption Standards**:
   - **At Rest**: AES-256 encryption across all PostgreSQL database volumes and backups managed via HSM/KMS.
   - **In Transit**: Mandatory TLS 1.3 for all ingress traffic and internal service-to-service communication.
3. **Data Minimization & Log Masking**:
   - Patient national identifiers and clinical referral reasons are classified as **Sensitive**. They are excluded from API status query responses, queue event payloads, and application log output.
4. **Immutable Audit Trails**:
   - The `audit_logs` table stores all access attempts, state changes, and DLQ replays for regulatory compliance.

---

## 5. Deployment Procedure

### 5.1 Docker Build
```bash
docker build \
  --build-arg APP_VERSION=0.1.0 \
  -t apex-referral-service:0.1.0 .
```

The image runs as an unprivileged user and contains only runtime dependencies,
application code, and Alembic migrations. The same immutable image is used for
the API, worker, and one-shot migration process.

Base images and the external `uv` binary are pinned by digest in the Dockerfile
and Compose file. Update those digests deliberately as part of dependency
maintenance, then rebuild and rerun the complete test suite.

### 5.2 Database Migrations (Pre-Deployment Step)
Run Alembic schema migrations before rolling out new application versions:
```bash
docker compose run --rm migrate
```

The Compose file is for local acceptance testing, not direct production
deployment. Copy `.env.example` to `.env`, replace its secret placeholders,
and start the packaged stack:

```bash
docker compose up --build -d
docker compose ps
docker compose logs migrate api worker redis
```

PostgreSQL, Redis, and API ports bind only to host loopback. Redis runs as its
built-in unprivileged user with all Linux capabilities dropped, and persistence
is disabled because limiter state is intentionally ephemeral. The migration service must
complete successfully before Compose starts the API or worker. Compose builds
one application image and reuses it across all three services. In a production
orchestrator, inject secrets from a managed secret store, use managed Redis with
authentication and TLS, do not publish database/cache ports, and preserve
migrations as a separate deployment job.

### 5.3 Blue/Green or Rolling Deployment
1. Deploy worker pods first to ensure new event schema compatibility.
2. Deploy API pods with zero-downtime rolling updates.
3. Use `/health/live` for liveness and `/health/ready` for traffic readiness.
   Readiness returns `503` when PostgreSQL cannot accept a query.
