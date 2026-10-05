"""Upgrade a database that holds pre-0007 production-shaped data and check nothing is lost or misread."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.helpers import TEST_DATABASE_URL

EVENT = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def scratch_url():
    base = make_url(TEST_DATABASE_URL)
    name = f"hackm_migration_{uuid4().hex[:8]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception:
        pytest.skip("test role cannot create databases")
    url = base.set(database=name)
    yield url.render_as_string(hide_password=False)
    create_engine(url).dispose()
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _config(url: str) -> Config:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_upgrade_preserves_paid_teams_resolves_pending_duplicates_and_moves_scores(scratch_url: str) -> None:
    command.upgrade(_config(scratch_url), "0006_admin_live_data")
    engine = create_engine(scratch_url)
    paid, old_pending, new_pending, judge = uuid4(), uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id, email_normalized, display_name, status, created_at, updated_at) VALUES (:id, 'judge@example.com', 'Judge', 'ACTIVE', now(), now())"), {"id": judge})
        for registration_id, team, status, leader, age in (
            (paid, "Alpha", "PAID", "alpha@example.com", "3 days"),
            (old_pending, "Beta", "PAYMENT_PENDING", "beta@example.com", "2 days"),
            (new_pending, "Gamma", "PAYMENT_PENDING", "beta@example.com", "1 day"),
        ):
            connection.execute(text(
                "INSERT INTO registrations (id, event_id, team_name, leader_email, status, quoted_amount_minor, pricing_tier, created_at) "
                f"VALUES (:id, :event, :team, :leader, :status, 20000, 'adamas-acm', now() - interval '{age}')"
            ), {"id": registration_id, "event": EVENT, "team": team, "leader": leader, "status": status})
            connection.execute(text(
                "INSERT INTO registration_members (id, registration_id, role, full_name, email_normalized, phone, college, is_acm_member) "
                "VALUES (:id, :reg, 'leader', 'Lead', :leader, '9812345678', 'Adamas University', true)"
            ), {"id": uuid4(), "reg": registration_id, "leader": leader})
            connection.execute(text(
                "INSERT INTO payment_orders (id, registration_id, provider_order_id, amount_minor, currency, status) VALUES (:id, :reg, :order, 20000, 'INR', :status)"
            ), {"id": uuid4(), "reg": registration_id, "order": f"order_{uuid4().hex[:10]}", "status": "CAPTURED" if status == "PAID" else "CREATED"})
        connection.execute(text(
            "INSERT INTO submissions (id, registration_id, status, project_name, github_url, demo_url, video_url, submitted_at, assigned_user_id) "
            "VALUES (:id, :reg, 'SUBMITTED', 'Alpha app', 'https://github.com/a/b', '', '', now(), :judge)"
        ), {"id": uuid4(), "reg": paid, "judge": judge})
        connection.execute(text(
            "INSERT INTO evaluations (id, registration_id, round, innovation, technical, problem_solving, ui_ux, completeness, evaluated_by_user_id, evaluated_at) "
            "VALUES (:id, :reg, 'round-1', 8, 7, 6, 5, 4, :judge, now())"
        ), {"id": uuid4(), "reg": paid, "judge": judge})

    command.upgrade(_config(scratch_url), "head")

    with engine.connect() as connection:
        statuses = dict(connection.execute(text("SELECT team_name, status FROM registrations")).all())
        assert statuses == {"Alpha": "PAID", "Beta": "ABANDONED", "Gamma": "PAYMENT_PENDING"}
        alpha = connection.execute(text("SELECT category, effective_amount_minor, adjustment_status FROM registrations WHERE id = :id"), {"id": paid}).one()
        assert tuple(alpha) == ("Adamas ACM", 20000, "NONE")
        assert connection.execute(text("SELECT acm_review_status FROM registration_members WHERE registration_id = :id"), {"id": paid}).scalar() == "PENDING"
        assert connection.execute(text("SELECT status FROM payment_orders WHERE registration_id = :id"), {"id": old_pending}).scalar() == "CANCELLED"
        assert connection.execute(text("SELECT anonymous_code FROM submissions WHERE registration_id = :id"), {"id": paid}).scalar().startswith("HM-")
        card = connection.execute(text("SELECT status, scores::text FROM scorecards WHERE registration_id = :id"), {"id": paid}).one()
        assert card.status == "SUBMITTED" and '"innovation": 8' in card.scores
        assert connection.execute(text("SELECT count(*) FROM evaluation_assignments WHERE registration_id = :id"), {"id": paid}).scalar() == 1
    engine.dispose()

    command.downgrade(_config(scratch_url), "0006_admin_live_data")
    command.upgrade(_config(scratch_url), "head")
