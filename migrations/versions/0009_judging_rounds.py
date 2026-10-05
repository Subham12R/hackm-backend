"""judging rounds, criteria, multi-judge assignments, scorecards, result snapshots

Revision ID: 0009_judging_rounds
Revises: 0008_event_control
Create Date: 2026-10-05
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0009_judging_rounds"
down_revision = "0008_event_control"
branch_labels = None
depends_on = None

EVENT_ID = "00000000-0000-4000-8000-000000000001"
LEGACY_CRITERIA = [
    ("innovation", "Innovation"),
    ("technical", "Technical implementation"),
    ("problem_solving", "Problem solving"),
    ("ui_ux", "UI/UX"),
    ("completeness", "Completeness"),
]


def upgrade() -> None:
    op.create_table(
        "evaluation_rounds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("scope", sa.String(length=20), nullable=False, server_default="ALL_SUBMITTED"),
        sa.Column("min_evaluations", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("event_id", "sequence"),
    )
    op.create_index("ix_evaluation_rounds_event_id", "evaluation_rounds", ["event_id"])
    op.create_table(
        "evaluation_criteria",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("round_id", sa.Uuid(), sa.ForeignKey("evaluation_rounds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(length=60), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("max_score", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("round_id", "key"),
        sa.CheckConstraint("max_score > 0", name="ck_criteria_max_positive"),
        sa.CheckConstraint("weight > 0", name="ck_criteria_weight_positive"),
    )
    op.create_index("ix_evaluation_criteria_round_id", "evaluation_criteria", ["round_id"])
    op.create_table(
        "round_entries",
        sa.Column("round_id", sa.Uuid(), sa.ForeignKey("evaluation_rounds.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id"), primary_key=True),
        sa.Column("added_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "evaluation_assignments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("round_id", sa.Uuid(), sa.ForeignKey("evaluation_rounds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("judge_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assigned_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("round_id", "registration_id", "judge_user_id"),
    )
    for column in ("round_id", "registration_id", "judge_user_id"):
        op.create_index(f"ix_evaluation_assignments_{column}", "evaluation_assignments", [column])
    op.create_table(
        "scorecards",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("assignment_id", sa.Uuid(), sa.ForeignKey("evaluation_assignments.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("round_id", sa.Uuid(), sa.ForeignKey("evaluation_rounds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("judge_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("scores", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("comments", sa.Text(), nullable=False, server_default=""),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    for column in ("round_id", "registration_id", "judge_user_id"):
        op.create_index(f"ix_scorecards_{column}", "scorecards", [column])
    op.create_table(
        "result_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("round_id", sa.Uuid(), sa.ForeignKey("evaluation_rounds.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("generated_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("locked_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("released_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
    )
    op.create_table(
        "result_rows",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("snapshot_id", sa.Uuid(), sa.ForeignKey("result_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id"), nullable=False),
        sa.Column("rank", sa.Integer()),
        sa.Column("score", sa.Float()),
        sa.Column("evaluations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ineligible_reason", sa.String(length=80)),
        sa.Column("breakdown", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("team_name", sa.String(length=200), nullable=False),
        sa.Column("project_name", sa.String(length=200), nullable=False, server_default=""),
        sa.UniqueConstraint("snapshot_id", "registration_id"),
    )
    op.create_index("ix_result_rows_snapshot_id", "result_rows", ["snapshot_id"])

    op.rename_table("evaluations", "evaluations_legacy")
    _migrate_legacy_scores()


def _migrate_legacy_scores() -> None:
    """Move single-judge assignments and old five-column scores into a default Round 1."""
    bind = op.get_bind()
    legacy = bind.execute(sa.text(
        "SELECT registration_id, round, innovation, technical, problem_solving, ui_ux, completeness, evaluated_by_user_id, evaluated_at "
        "FROM evaluations_legacy"
    )).all()
    assigned = bind.execute(sa.text("SELECT registration_id, assigned_user_id FROM submissions WHERE assigned_user_id IS NOT NULL")).all()
    if not legacy and not assigned:
        return
    now = datetime.now(UTC)
    round_id = uuid4()
    bind.execute(sa.text(
        "INSERT INTO evaluation_rounds (id, event_id, name, sequence, status, scope, min_evaluations, created_at) "
        "VALUES (:id, :event, 'Round 1', 1, 'OPEN', 'ALL_SUBMITTED', 1, :now)"
    ), {"id": round_id, "event": EVENT_ID, "now": now})
    for position, (key, label) in enumerate(LEGACY_CRITERIA):
        bind.execute(sa.text(
            "INSERT INTO evaluation_criteria (id, round_id, key, label, max_score, weight, position) VALUES (:id, :round, :key, :label, 10, 1, :position)"
        ), {"id": uuid4(), "round": round_id, "key": key, "label": label, "position": position})
    assignments: dict[tuple, object] = {}

    def assignment(registration_id, judge_id):
        key = (registration_id, judge_id)
        if key not in assignments:
            assignments[key] = uuid4()
            bind.execute(sa.text(
                "INSERT INTO evaluation_assignments (id, round_id, registration_id, judge_user_id, assigned_at) VALUES (:id, :round, :reg, :judge, :now)"
            ), {"id": assignments[key], "round": round_id, "reg": registration_id, "judge": judge_id, "now": now})
        return assignments[key]

    for row in assigned:
        assignment(row.registration_id, row.assigned_user_id)
    for row in legacy:
        assignment_id = assignment(row.registration_id, row.evaluated_by_user_id)
        scores = {key: getattr(row, key) for key, _label in LEGACY_CRITERIA}
        bind.execute(sa.text(
            "INSERT INTO scorecards (id, assignment_id, round_id, registration_id, judge_user_id, status, scores, comments, submitted_at, updated_at) "
            "VALUES (:id, :assignment, :round, :reg, :judge, 'SUBMITTED', CAST(:scores AS JSON), :comments, :at, :at) ON CONFLICT (assignment_id) DO NOTHING"
        ), {"id": uuid4(), "assignment": assignment_id, "round": round_id, "reg": row.registration_id, "judge": row.evaluated_by_user_id,
            "scores": json.dumps(scores), "comments": f"Imported from legacy round '{row.round}'", "at": row.evaluated_at})


def downgrade() -> None:
    op.rename_table("evaluations_legacy", "evaluations")
    op.drop_index("ix_result_rows_snapshot_id", table_name="result_rows")
    op.drop_table("result_rows")
    op.drop_table("result_snapshots")
    for column in ("round_id", "registration_id", "judge_user_id"):
        op.drop_index(f"ix_scorecards_{column}", table_name="scorecards")
    op.drop_table("scorecards")
    for column in ("round_id", "registration_id", "judge_user_id"):
        op.drop_index(f"ix_evaluation_assignments_{column}", table_name="evaluation_assignments")
    op.drop_table("evaluation_assignments")
    op.drop_table("round_entries")
    op.drop_index("ix_evaluation_criteria_round_id", table_name="evaluation_criteria")
    op.drop_table("evaluation_criteria")
    op.drop_index("ix_evaluation_rounds_event_id", table_name="evaluation_rounds")
    op.drop_table("evaluation_rounds")
