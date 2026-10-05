import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.auth.policies import RoleCode
from src.auth.security import hash_password
from src.db.models.identity import AuditLog, Role, User, UserStatus
from src.db.models.registration import (
    PaymentAttempt,
    Refund,
    PaymentOrder,
    Registration,
    RegistrationMember,
    Submission,
)
from src.main import create_app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test",
)


def test_team_review_defaults_to_pending_after_migration() -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    private_key = Ed25519PrivateKey.generate()
    app = create_app(
        database_url=TEST_DATABASE_URL,
        jwt_private_key=private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        ).decode(),
        jwt_public_key=private_key.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        ).decode(),
    )
    admin_email = f"admin-{uuid4()}@example.com"
    leader_email = f"leader-{uuid4()}@example.com"
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        session.add(
            User(
                email_normalized=admin_email,
                display_name="Admin",
                password_hash=hash_password("password"),
                status=UserStatus.ACTIVE,
                roles=[session.get(Role, RoleCode.SUPER_ADMIN)],
            )
        )
        registration = Registration(
            event_id="00000000-0000-4000-8000-000000000001",
            team_name=f"Team {uuid4()}",
            leader_email=leader_email,
            status="PAID",
            quoted_amount_minor=20000,
            pricing_tier="adamas-acm",
        )
        session.add(registration)
        session.flush()
        session.add(
            RegistrationMember(
                registration_id=registration.id,
                role="leader",
                full_name="Leader",
                email_normalized=leader_email,
                phone="9812345678",
                college="Adamas University",
                is_acm_member=True,
            )
        )
        session.commit()

    client = TestClient(app)
    access_token = client.post(
        "/api/v1/auth/login",
        json={"email": admin_email, "password": "password", "portal": "admin"},
    ).json()["tokens"]["access_token"]
    response = client.get(
        "/api/v1/admin/teams?page=1&page_size=10",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    team = response.json()["items"][0]
    assert team["review"] == {
        "status": "PENDING",
        "reviewed_at": None,
        "reviewed_by": None,
    }


def _client() -> TestClient:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    private_key = Ed25519PrivateKey.generate()
    return TestClient(
        create_app(
            database_url=TEST_DATABASE_URL,
            jwt_private_key=private_key.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
            ).decode(),
            jwt_public_key=private_key.public_key().public_bytes(
                Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
            ).decode(),
        )
    )


def _access_token(client: TestClient, email: str) -> str:
    return client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "password", "portal": "admin"},
    ).json()["tokens"]["access_token"]


def test_registration_filter_pagination_and_daily_counts_are_server_owned() -> None:
    client = _client()
    admin_email = f"admin-{uuid4()}@example.com"
    unique_day = datetime(2100, 1, 1, tzinfo=UTC) + timedelta(days=uuid4().int % 365)
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        session.add(
            User(
                email_normalized=admin_email,
                display_name="Admin",
                password_hash=hash_password("password"),
                status=UserStatus.ACTIVE,
                roles=[session.get(Role, RoleCode.SUPER_ADMIN)],
            )
        )
        for team_name, college, is_acm_member in [
            ("Adamas team", "Adamas University", True),
            ("Outer team", "Other College", False),
        ]:
            email = f"leader-{uuid4()}@example.com"
            registration = Registration(
                event_id="00000000-0000-4000-8000-000000000001",
                team_name=f"{team_name} {uuid4()}",
                leader_email=email,
                status="PAID",
                quoted_amount_minor=20000,
                pricing_tier="standard",
                created_at=unique_day,
            )
            session.add(registration)
            session.flush()
            session.add(
                RegistrationMember(
                    registration_id=registration.id,
                    role="leader",
                    full_name="Leader",
                    email_normalized=email,
                    phone="9812345678",
                    college=college,
                    is_acm_member=is_acm_member,
                )
            )
        session.commit()

    response = client.get(
        f"/api/v1/admin/registrations?date={unique_day.date().isoformat()}&category=Adamas%20ACM&page=1&page_size=1",
        headers={"Authorization": f"Bearer {_access_token(client, admin_email)}"},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["college"] == "Adamas University"
    assert response.json()["daily_counts"] == [
        {"date": unique_day.date().isoformat(), "adamas": 1, "other": 0}
    ]


def test_judge_cannot_read_identity_bearing_admin_tables() -> None:
    client = _client()
    judge_email = f"judge-{uuid4()}@example.com"
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        session.add(
            User(
                email_normalized=judge_email,
                display_name="Judge",
                password_hash=hash_password("password"),
                status=UserStatus.ACTIVE,
                roles=[session.get(Role, RoleCode.JUDGE)],
            )
        )
        session.commit()

    response = client.get(
        "/api/v1/admin/teams?page=1&page_size=10",
        headers={"Authorization": f"Bearer {_access_token(client, judge_email)}"},
    )

    assert response.status_code == 403


def test_overview_uses_captured_money_and_shared_recent_rows() -> None:
    client = _client()
    admin_email = f"admin-{uuid4()}@example.com"
    created_at = datetime(2200, 1, 1, tzinfo=UTC) + timedelta(days=uuid4().int % 365)
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        admin = User(
            email_normalized=admin_email,
            display_name="Overview Admin",
            password_hash=hash_password("password"),
            status=UserStatus.ACTIVE,
            roles=[session.get(Role, RoleCode.SUPER_ADMIN)],
        )
        session.add(admin)
        session.flush()
        captured = Registration(
            event_id="00000000-0000-4000-8000-000000000001",
            team_name=f"Captured {uuid4()}",
            leader_email=f"captured-{uuid4()}@example.com",
            status="PAID",
            quoted_amount_minor=20000,
            pricing_tier="standard",
            created_at=created_at,
        )
        pending = Registration(
            event_id="00000000-0000-4000-8000-000000000001",
            team_name=f"Pending {uuid4()}",
            leader_email=f"pending-{uuid4()}@example.com",
            status="PAYMENT_PENDING",
            quoted_amount_minor=90000,
            pricing_tier="standard",
            created_at=created_at - timedelta(days=1),
        )
        session.add_all([captured, pending])
        session.flush()
        captured_id = str(captured.id)
        session.add_all(
            [
                PaymentOrder(
                    registration_id=captured.id,
                    provider_order_id=f"order_{uuid4().hex}",
                    amount_minor=20000,
                    currency="INR",
                    status="CAPTURED",
                ),
                PaymentOrder(
                    registration_id=pending.id,
                    provider_order_id=f"order_{uuid4().hex}",
                    amount_minor=90000,
                    currency="INR",
                    status="CREATED",
                ),
                AuditLog(
                    actor_user_id=admin.id,
                    action="Overview test",
                    target_type="registration",
                    target_id=captured_id,
                    metadata_json={},
                    created_at=created_at,
                ),
            ]
        )
        session.commit()
        expected_metrics = {
            "registrations": session.scalar(
                select(func.count(Registration.id)).where(
                    Registration.status.in_(("PAYMENT_PENDING", "PAID", "REFUNDED"))
                )
            ),
            "paid_teams": session.scalar(
                select(func.count(Registration.id)).where(Registration.status == "PAID")
            ),
            # Net of processed refunds; this test seeds none, so it equals captured money.
            "monetary_amount_minor": session.scalar(
                select(func.coalesce(func.sum(PaymentOrder.amount_minor), 0)).where(
                    PaymentOrder.status == "CAPTURED"
                )
            )
            - session.scalar(
                select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
                    Refund.status == "PROCESSED"
                )
            ),
            # Finalized submissions, not paid registrations.
            "submissions": session.scalar(
                select(func.count(Submission.id)).where(Submission.status == "SUBMITTED")
            ),
            "total_users": session.scalar(select(func.count(User.id))),
            "payments_needing_review": session.scalar(
                select(func.count(PaymentAttempt.id)).where(PaymentAttempt.status == "NEEDS_REVIEW")
            ),
        }

    response = client.get(
        "/api/v1/admin/overview",
        headers={"Authorization": f"Bearer {_access_token(client, admin_email)}"},
    )

    assert response.status_code == 200
    overview = response.json()
    assert overview["viewer_name"] == "Overview Admin"
    assert overview["metrics"] == expected_metrics
    assert captured_id in {
        row["registration_id"] for row in overview["recent_registrations"]
    }
    assert "Overview test" in {row["action"] for row in overview["recent_logs"]}


def test_registration_admin_can_review_once_and_audit_the_change() -> None:
    client = _client()
    admin_email = f"registration-admin-{uuid4()}@example.com"
    leader_email = f"leader-{uuid4()}@example.com"
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        session.add(
            User(
                email_normalized=admin_email,
                display_name="Registration Admin",
                password_hash=hash_password("password"),
                status=UserStatus.ACTIVE,
                roles=[session.get(Role, RoleCode.REGISTRATION_ADMIN)],
            )
        )
        registration = Registration(
            event_id="00000000-0000-4000-8000-000000000001",
            team_name=f"Review {uuid4()}",
            leader_email=leader_email,
            status="PAID",
            quoted_amount_minor=20000,
            pricing_tier="standard",
        )
        session.add(registration)
        session.flush()
        registration_id = str(registration.id)
        session.commit()

    headers = {"Authorization": f"Bearer {_access_token(client, admin_email)}"}
    first = client.post(
        f"/api/v1/admin/teams/{registration_id}/review",
        headers=headers,
        json={"status": "VERIFIED"},
    )
    repeated = client.post(
        f"/api/v1/admin/teams/{registration_id}/review",
        headers=headers,
        json={"status": "VERIFIED"},
    )

    assert first.status_code == 200
    assert first.json()["review"]["status"] == "VERIFIED"
    assert repeated.status_code == 200
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        assert session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "Team details reviewed",
                AuditLog.target_id == registration_id,
            )
        ) == 1


def test_assignment_requires_active_judge_and_evaluation_capability() -> None:
    client = _client()
    evaluator_email = f"evaluator-admin-{uuid4()}@example.com"
    registration_admin_email = f"registration-admin-{uuid4()}@example.com"
    judge_email = f"judge-{uuid4()}@example.com"
    inactive_judge_email = f"inactive-judge-{uuid4()}@example.com"
    non_judge_email = f"non-judge-{uuid4()}@example.com"
    with Session(create_engine(TEST_DATABASE_URL)) as session:
        evaluation_admin_role = session.get(Role, RoleCode.EVALUATION_ADMIN)
        registration_admin_role = session.get(Role, RoleCode.REGISTRATION_ADMIN)
        judge_role = session.get(Role, RoleCode.JUDGE)
        viewer_role = session.get(Role, RoleCode.VIEWER)
        session.add_all(
            [
                User(
                    email_normalized=evaluator_email,
                    display_name="Evaluator Admin",
                    password_hash=hash_password("password"),
                    status=UserStatus.ACTIVE,
                    roles=[evaluation_admin_role],
                ),
                User(
                    email_normalized=registration_admin_email,
                    display_name="Registration Admin",
                    password_hash=hash_password("password"),
                    status=UserStatus.ACTIVE,
                    roles=[registration_admin_role],
                ),
                User(
                    email_normalized=judge_email,
                    display_name="Active Judge",
                    password_hash=hash_password("password"),
                    status=UserStatus.ACTIVE,
                    roles=[judge_role],
                ),
                User(
                    email_normalized=inactive_judge_email,
                    display_name="Inactive Judge",
                    password_hash=hash_password("password"),
                    status=UserStatus.DISABLED,
                    roles=[judge_role],
                ),
                User(
                    email_normalized=non_judge_email,
                    display_name="Non Judge",
                    password_hash=hash_password("password"),
                    status=UserStatus.ACTIVE,
                    roles=[viewer_role],
                ),
            ]
        )
        registration = Registration(
            event_id="00000000-0000-4000-8000-000000000001",
            team_name=f"Submission {uuid4()}",
            leader_email=f"leader-{uuid4()}@example.com",
            status="PAID",
            quoted_amount_minor=20000,
            pricing_tier="standard",
        )
        session.add(registration)
        session.flush()
        session.add(
            Submission(
                registration_id=registration.id,
                status="SUBMITTED",
                project_name="Project",
            )
        )
        registration_id = str(registration.id)
        session.commit()

    evaluator_headers = {
        "Authorization": f"Bearer {_access_token(client, evaluator_email)}"
    }
    registration_headers = {
        "Authorization": f"Bearer {_access_token(client, registration_admin_email)}"
    }
    evaluators = client.get("/api/v1/admin/evaluators", headers=evaluator_headers)
    denied = client.post(
        f"/api/v1/admin/submissions/{registration_id}/assign",
        headers=registration_headers,
        json={"assignee_email": judge_email},
    )
    non_judge = client.post(
        f"/api/v1/admin/submissions/{registration_id}/assign",
        headers=evaluator_headers,
        json={"assignee_email": non_judge_email},
    )
    assigned = client.post(
        f"/api/v1/admin/submissions/{registration_id}/assign",
        headers=evaluator_headers,
        json={"assignee_email": judge_email},
    )

    assert evaluators.status_code == 200
    assert {row["email"] for row in evaluators.json()["evaluators"]} >= {judge_email}
    assert inactive_judge_email not in {
        row["email"] for row in evaluators.json()["evaluators"]
    }
    assert denied.status_code == 403
    assert non_judge.status_code == 404
    assert assigned.json()["assigned_to"] == {
        "email": judge_email,
        "display_name": "Active Judge",
    }
