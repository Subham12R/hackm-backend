"""multiple events (stages) with tracks, per-stage submissions and awards

Revision ID: 0012_stages
Revises: 0011_gallery_checkin
Create Date: 2026-10-09

Every existing event gets one published "Main round" stage that takes over its
submission window and reveal time; its challenge becomes that stage's first track,
and existing submissions and judging rounds move into it. The old event columns
and the challenges table are left in place (unused) so a rollback loses nothing.
"""

import json
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0012_stages"
down_revision = "0011_gallery_checkin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligibility", sa.String(length=20), nullable=False, server_default="ALL_PAID"),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("submission_opens_at", sa.DateTime(timezone=True)),
        sa.Column("submission_closes_at", sa.DateTime(timezone=True)),
        sa.Column("statements_release_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("updated_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
    )
    op.create_index("ix_stages_event_id", "stages", ["event_id"])
    op.create_table(
        "tracks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("stage_id", sa.Uuid(), sa.ForeignKey("stages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("resources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
    )
    op.create_index("ix_tracks_stage_id", "tracks", ["stage_id"])

    bind = op.get_bind()
    events = bind.execute(sa.text("select id, submission_opens_at, submission_closes_at, challenge_release_at from events")).all()
    for event_id, opens_at, closes_at, release_at in events:
        stage_id = uuid.uuid4()
        bind.execute(sa.text(
            "insert into stages (id, event_id, name, description, position, eligibility, published, submission_opens_at, submission_closes_at, statements_release_at)"
            " values (:id, :event_id, 'Main round', '', 1, 'ALL_PAID', true, :opens, :closes, :release)"
        ), {"id": stage_id, "event_id": event_id, "opens": opens_at, "closes": closes_at, "release": release_at})
        challenge = bind.execute(sa.text(
            "select title, summary, body, resources, version, updated_at, updated_by_user_id from challenges where event_id = :event_id"
        ), {"event_id": event_id}).first()
        if challenge is not None and (challenge.title or challenge.body):
            bind.execute(sa.text(
                "insert into tracks (id, stage_id, title, summary, body, resources, position, version, updated_at, updated_by_user_id)"
                " values (:id, :stage_id, :title, :summary, :body, cast(:resources as json), 1, :version, :updated_at, :by)"
            ), {"id": uuid.uuid4(), "stage_id": stage_id, "title": challenge.title or "Problem statement", "summary": challenge.summary,
                "body": challenge.body, "resources": _json(challenge.resources),
                "version": challenge.version, "updated_at": challenge.updated_at, "by": challenge.updated_by_user_id})

    op.add_column("submissions", sa.Column("stage_id", sa.Uuid(), sa.ForeignKey("stages.id")))
    op.add_column("submissions", sa.Column("track_id", sa.Uuid(), sa.ForeignKey("tracks.id", ondelete="SET NULL")))
    op.execute(
        "update submissions s set stage_id = st.id from registrations r, stages st"
        " where r.id = s.registration_id and st.event_id = r.event_id"
    )
    # A migrated challenge is the stage's only track, so every existing submission was made for it.
    op.execute(
        "update submissions s set track_id = t.id from tracks t where t.stage_id = s.stage_id"
    )
    op.alter_column("submissions", "stage_id", nullable=False)
    op.drop_constraint("submissions_registration_id_key", "submissions", type_="unique")
    op.create_unique_constraint("uq_submissions_registration_stage", "submissions", ["registration_id", "stage_id"])
    op.create_index("ix_submissions_registration_id", "submissions", ["registration_id"])
    op.create_index("ix_submissions_stage_id", "submissions", ["stage_id"])

    op.add_column("evaluation_rounds", sa.Column("stage_id", sa.Uuid(), sa.ForeignKey("stages.id")))
    op.execute("update evaluation_rounds er set stage_id = st.id from stages st where st.event_id = er.event_id")
    op.alter_column("evaluation_rounds", "stage_id", nullable=False)
    op.create_index("ix_evaluation_rounds_stage_id", "evaluation_rounds", ["stage_id"])

    op.create_table(
        "submission_awards",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("stage_id", sa.Uuid(), sa.ForeignKey("stages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("track_id", sa.Uuid(), sa.ForeignKey("tracks.id", ondelete="CASCADE")),
        sa.Column("submission_id", sa.Uuid(), sa.ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("placement", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("awarded_by_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("awarded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("stage_id", "track_id", "placement", name="uq_submission_awards_placement", postgresql_nulls_not_distinct=True),
        sa.UniqueConstraint("submission_id", "track_id", name="uq_submission_awards_submission", postgresql_nulls_not_distinct=True),
    )
    op.create_index("ix_submission_awards_stage_id", "submission_awards", ["stage_id"])
    op.create_index("ix_submission_awards_submission_id", "submission_awards", ["submission_id"])


def _json(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value or [])


def downgrade() -> None:
    op.drop_index("ix_submission_awards_submission_id", table_name="submission_awards")
    op.drop_index("ix_submission_awards_stage_id", table_name="submission_awards")
    op.drop_table("submission_awards")
    op.drop_index("ix_evaluation_rounds_stage_id", table_name="evaluation_rounds")
    op.drop_column("evaluation_rounds", "stage_id")
    # Only the first stage's submissions fit the old one-per-team shape.
    op.execute(
        "delete from submissions s using stages st where st.id = s.stage_id"
        " and st.position > (select min(position) from stages f where f.event_id = st.event_id)"
    )
    op.drop_index("ix_submissions_stage_id", table_name="submissions")
    op.drop_index("ix_submissions_registration_id", table_name="submissions")
    op.drop_constraint("uq_submissions_registration_stage", "submissions", type_="unique")
    op.create_unique_constraint("submissions_registration_id_key", "submissions", ["registration_id"])
    op.drop_column("submissions", "track_id")
    op.drop_column("submissions", "stage_id")
    op.drop_index("ix_tracks_stage_id", table_name="tracks")
    op.drop_table("tracks")
    op.drop_index("ix_stages_event_id", table_name="stages")
    op.drop_table("stages")
