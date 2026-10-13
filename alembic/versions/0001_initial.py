"""Initial schema — all tables.

Revision ID: 0001_initial
Revises: —
Create Date: 2026-09-24

Creates:
  referrals
  idempotency_records
  outbox_events
  integration_events
  audit_logs
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Enums ---
    referral_status = postgresql.ENUM(
        "RECEIVED", "PROCESSING", "ELIGIBILITY_FAILED",
        "ROUTED", "CRM_PENDING", "COMPLETED", "DEAD_LETTER",
        name="referral_status",
    )
    processing_step = postgresql.ENUM(
        "INTAKE", "EHR_ELIGIBILITY", "AI_CLASSIFICATION",
        "ROUTING", "CRM", "COMPLETE",
        name="processing_step",
    )
    outbox_status = postgresql.ENUM(
        "PENDING", "PUBLISHED", "CONSUMED",
        name="outbox_status",
    )
    integration_step = postgresql.ENUM(
        "EHR_ELIGIBILITY", "AI_CLASSIFICATION", "ROUTING", "CRM",
        name="integration_step",
    )
    integration_outcome = postgresql.ENUM(
        "PENDING", "SUCCESS", "FAILED_RETRYABLE",
        "FAILED_NON_RETRYABLE", "SKIPPED",
        name="integration_outcome",
    )

    referral_status.create(op.get_bind(), checkfirst=True)
    processing_step.create(op.get_bind(), checkfirst=True)
    outbox_status.create(op.get_bind(), checkfirst=True)
    integration_step.create(op.get_bind(), checkfirst=True)
    integration_outcome.create(op.get_bind(), checkfirst=True)

    # --- referrals ---
    op.create_table(
        "referrals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("referral_id", sa.String(64), nullable=False),
        sa.Column("external_reference", sa.String(128), nullable=False),
        sa.Column("patient_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("specialty", sa.String(64), nullable=False),
        sa.Column("preferred_hospital", sa.String(64), nullable=False),
        sa.Column("hospital", sa.String(64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "RECEIVED", "PROCESSING", "ELIGIBILITY_FAILED",
                "ROUTED", "CRM_PENDING", "COMPLETED", "DEAD_LETTER",
                name="referral_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "current_step",
            sa.Enum(
                "INTAKE", "EHR_ELIGIBILITY",
                "AI_CLASSIFICATION", "ROUTING",
                "CRM", "COMPLETE",
                name="processing_step",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("eligibility_reference", sa.String(128), nullable=True),
        sa.Column("ai_classification", sa.String(64), nullable=True),
        sa.Column("ai_urgency", sa.String(32), nullable=True),
        sa.Column("ai_confidence", sa.Float(), nullable=True),
        sa.Column("routing_reason", sa.String(128), nullable=True),
        sa.Column("crm_case_id", sa.String(64), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failed_step", sa.String(32), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_referrals_referral_id", "referrals", ["referral_id"], unique=True)
    op.create_index("ix_referrals_external_reference", "referrals", ["external_reference"])
    op.create_index("ix_referrals_status", "referrals", ["status"])
    op.create_index("ix_referrals_client_hospital", "referrals", ["client_id", "preferred_hospital"])

    # --- idempotency_records ---
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("referral_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "idempotency_key", name="uq_client_idempotency"),
    )

    # --- outbox_events ---
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("referral_id", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "PUBLISHED", "CONSUMED", name="outbox_status", create_type=False),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index("ix_outbox_events_referral_id", "outbox_events", ["referral_id"])
    op.create_index("ix_outbox_pending", "outbox_events", ["status", "created_at"])

    # --- integration_events ---
    op.create_table(
        "integration_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("referral_id", sa.String(64), nullable=False),
        sa.Column(
            "step",
            sa.Enum(
                "EHR_ELIGIBILITY", "AI_CLASSIFICATION",
                "ROUTING", "CRM",
                name="integration_step",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "outcome",
            sa.Enum(
                "PENDING", "SUCCESS", "FAILED_RETRYABLE",
                "FAILED_NON_RETRYABLE", "SKIPPED",
                name="integration_outcome",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result_reference", sa.String(256), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_integration_referral_step", "integration_events", ["referral_id", "step"])

    # --- audit_logs ---
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_timestamp", "audit_logs", ["timestamp"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_resource_id", "audit_logs", ["resource_id"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("integration_events")
    op.drop_table("outbox_events")
    op.drop_table("idempotency_records")
    op.drop_table("referrals")

    op.execute("DROP TYPE IF EXISTS integration_outcome")
    op.execute("DROP TYPE IF EXISTS integration_step")
    op.execute("DROP TYPE IF EXISTS outbox_status")
    op.execute("DROP TYPE IF EXISTS processing_step")
    op.execute("DROP TYPE IF EXISTS referral_status")
