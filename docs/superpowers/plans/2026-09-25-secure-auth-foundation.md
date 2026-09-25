# Secure Authentication Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace temporary participant auth and unauthenticated admin access with FastAPI-issued Argon2id/EdDSA JWT authentication, PostgreSQL refresh sessions, resets, admin invites, and protected Next.js portal routes.

**Architecture:** FastAPI owns users, roles, password hashes, EdDSA JWT issuance, and hashed rotating refresh sessions in Neon PostgreSQL. Each Next.js app has a same-origin BFF route that calls FastAPI server-to-server and sets its own host-only HTTP-only access/refresh cookies; the browser never receives raw tokens through JavaScript. This phase has no registration payment, Razorpay order, or activation flow; those are Phase 2.

**Tech Stack:** Python 3.13, FastAPI, Pydantic Settings, SQLAlchemy 2, psycopg 3, Alembic, Argon2id, PyJWT with cryptography, PostgreSQL/Neon, pytest/httpx, Next.js 16 App Router.

**Spec:** `docs/PRD.md`, `CONTEXT.md`, `docs/folder-structure.md`

## Global Constraints

- Use Argon2id only; do not add bcrypt fallback.
- Access JWTs use Ed25519/EdDSA, `kid`, `iss`, `aud`, a 15-minute expiry, and no PII beyond immutable user ID/roles/session ID.
- Refresh tokens are opaque, random, hashed in PostgreSQL, rotated on every refresh, expire after 30 days, and expire after 14 days of inactivity.
- Participant and admin cookies are host-only, `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` outside local development.
- Every mutation validates the expected origin and returns a stable error envelope with a request ID.
- Use Neon through `DATABASE_URL`; no SQLite fallback. The same Alembic migrations must work against later Docker PostgreSQL.
- Resend is production email; a local sink stores redacted messages for development/tests.
- Seed the initial `SUPER_ADMIN` only from ignored configuration/interactive input. Do not commit credentials or secret keys.
- Do not commit while this repository contains the user-owned initial uncommitted files.
- Do not edit Razorpay/mock-payment/registration behavior in this phase.

---

## File structure

```text
hackm-backend/
├── src/
│   ├── main.py
│   ├── config.py
│   ├── api/{dependencies.py,errors.py,middleware.py,v1/{router.py,health.py,auth.py}}
│   ├── db/{base.py,session.py,models/identity.py}
│   ├── auth/{schemas.py,security.py,sessions.py,service.py,policies.py,rate_limits.py}
│   ├── communications/{outbox.py,providers/{email.py,sink.py,resend.py}}
│   └── audit/service.py
├── migrations/versions/<timestamp>_identity_auth.py
├── scripts/{create_admin.py,seed_dev.py}
└── tests/{unit/auth/,integration/}
```

The Next integration tasks modify only auth/guard code in `/Users/subham/Dev/hackm/frontend` and `/Users/subham/Dev/admin-hackm`. Preserve unrelated user-owned changes in those repositories.

### Task 1: Typed settings, dependencies, health, and database lifecycle

**Files:**
- Modify: `pyproject.toml`
- Modify: `main.py`
- Create: `src/__init__.py`
- Create: `src/config.py`
- Create: `src/db/base.py`
- Create: `src/db/session.py`
- Create: `src/api/errors.py`
- Create: `src/api/middleware.py`
- Create: `src/api/v1/health.py`
- Create: `src/api/v1/router.py`
- Create: `.env.example`
- Test: `tests/integration/test_health.py`

**Interfaces:**
- Produces `Settings`, `create_app()`, `get_db_session()`, and `GET /healthz`.
- Later tasks consume `Settings.database_url`, a synchronous SQLAlchemy `Session`, and the API error shape.

- [ ] **Step 1: Write the failing health test**

```python
from fastapi.testclient import TestClient

from hackm_backend.main import create_app


def test_health_reports_database_unavailable_without_leaking_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://invalid")
    response = TestClient(create_app()).get("/healthz")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "postgresql" not in response.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/integration/test_health.py -v`

Expected: FAIL because `hackm_backend.main` does not exist.

- [ ] **Step 3: Add runtime dependencies and the minimal application factory**

Add these production dependencies to `pyproject.toml`: `alembic`, `argon2-cffi`, `cryptography`, `pydantic-settings`, `psycopg[binary]`, `PyJWT`, and `sqlalchemy`. Add `pytest` and `httpx` to the development dependency group. Use `uv lock` after editing.

Implement settings with required environment values and no hardcoded fallback secrets:

```python
class Settings(BaseSettings):
    database_url: PostgresDsn
    jwt_issuer: str
    jwt_audience: str
    jwt_key_id: str
    jwt_private_key_pem: SecretStr
    jwt_public_keys_json: SecretStr
    access_ttl_seconds: int = 900
    refresh_ttl_days: int = 30
    refresh_idle_days: int = 14
    cookie_secure: bool = True
```

`create_app()` must attach request-ID middleware, include `/api/v1`, and expose `/healthz`. The health route runs `SELECT 1` through `get_db_session()` and returns only `{ "status": "ok" }` or the stable `database_unavailable` error.

- [ ] **Step 4: Run targeted checks**

Run: `uv lock && uv run pytest tests/integration/test_health.py -v && uv run python -m compileall src`

Expected: PASS.

### Task 2: Identity schema, Alembic migration, and privileged bootstrap

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/<timestamp>_identity_auth.py`
- Create: `src/db/models/__init__.py`
- Create: `src/db/models/identity.py`
- Create: `src/auth/policies.py`
- Create: `scripts/create_admin.py`
- Test: `tests/integration/test_identity_migration.py`
- Test: `tests/integration/test_admin_bootstrap.py`

**Interfaces:**
- Consumes `Base` and the database session from Task 1.
- Produces `User`, `Role`, `UserRole`, `RefreshSession`, `PasswordResetToken`, `AdminInvite`, and `AuditLog` mapped models.
- Produces `create_super_admin(email: str, password: str) -> User` and canonical role codes.

- [ ] **Step 1: Write failing schema/bootstrap tests**

```python
from sqlalchemy import select

from hackm_backend.auth.policies import RoleCode
from hackm_backend.db.models.identity import Role, User
from scripts.create_admin import create_super_admin


def test_bootstrap_is_idempotent_and_grants_only_super_admin(db_session):
    first = create_super_admin(db_session, "ADMIN@EXAMPLE.COM", "correct horse battery staple")
    second = create_super_admin(db_session, "admin@example.com", "correct horse battery staple")

    assert first.id == second.id
    assert db_session.scalar(select(User).where(User.email_normalized == "admin@example.com"))
    assert db_session.scalar(select(Role).where(Role.code == RoleCode.SUPER_ADMIN))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_identity_migration.py tests/integration/test_admin_bootstrap.py -v`

Expected: FAIL because models/migrations/bootstrap do not exist.

- [ ] **Step 3: Implement schema and migration**

Create enum-backed tables with database constraints:

```python
class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email_normalized: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str | None]
    status: Mapped[UserStatus]

class RefreshSession(Base):
    __tablename__ = "refresh_sessions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
```

Create roles `SUPER_ADMIN`, `EVENT_ADMIN`, `REGISTRATION_ADMIN`, `EVALUATION_ADMIN`, `SUPPORT_ADMIN`, `JUDGE`, and `VIEWER`; grant only `SUPER_ADMIN` in the bootstrap command. The command accepts the email argument and password through `getpass.getpass()`, normalizes the email, and never prints the password/hash.

- [ ] **Step 4: Apply migration to Neon test database and run tests**

Run: `uv run alembic upgrade head && uv run pytest tests/integration/test_identity_migration.py tests/integration/test_admin_bootstrap.py -v`

Expected: PASS; a second bootstrap does not create a second user or role link.

### Task 3: Argon2id passwords, EdDSA access JWTs, rotating refresh sessions, and policies

**Files:**
- Create: `src/auth/security.py`
- Create: `src/auth/sessions.py`
- Create: `src/auth/schemas.py`
- Create: `src/auth/service.py`
- Create: `src/auth/rate_limits.py`
- Test: `tests/unit/auth/test_security.py`
- Test: `tests/unit/auth/test_sessions.py`
- Test: `tests/unit/auth/test_policies.py`

**Interfaces:**
- Consumes Task 2 models/roles and Task 1 settings.
- Produces `hash_password()`, `verify_and_rehash_password()`, `issue_access_token()`, `decode_access_token()`, `create_refresh_session()`, `rotate_refresh_session()`, `revoke_refresh_session()`, and `require_portal_access()`.

- [ ] **Step 1: Write failing security tests**

```python
from datetime import timedelta

import pytest

from hackm_backend.auth.security import decode_access_token, hash_password, issue_access_token, verify_and_rehash_password


def test_password_uses_argon2id_and_never_returns_plaintext():
    password_hash = hash_password("correct horse battery staple")
    assert password_hash.startswith("$argon2id$")
    assert verify_and_rehash_password("correct horse battery staple", password_hash).valid
    assert not verify_and_rehash_password("wrong", password_hash).valid


def test_access_token_requires_expected_issuer_audience_and_expiry(user):
    token = issue_access_token(user, session_id="session-id")
    claims = decode_access_token(token)
    assert claims["sub"] == str(user.id)
    assert claims["sid"] == "session-id"

    with pytest.raises(Exception):
        decode_access_token(token, audience="wrong-audience")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/auth/test_security.py tests/unit/auth/test_sessions.py tests/unit/auth/test_policies.py -v`

Expected: FAIL because security/session services do not exist.

- [ ] **Step 3: Implement the security primitives**

Use `argon2.PasswordHasher` with Argon2id. Hash opaque refresh tokens with SHA-256 only after generating them using `secrets.token_urlsafe(48)`. Sign access claims using PyJWT `algorithm="EdDSA"` and include `sub`, `sid`, `roles`, `iss`, `aud`, `iat`, `exp`, and `jti`; set the key ID header.

Rotation must revoke the old session in the same transaction before inserting the replacement session:

```python
def rotate_refresh_session(session: Session, raw_token: str, now: datetime) -> IssuedSession:
    current = find_valid_refresh_session(session, raw_token, now)
    current.revoked_at = now
    current.last_seen_at = now
    replacement = create_refresh_session(session, current.user, now)
    return replacement
```

Implement portal policy as `participant` requiring a participant role and `admin` requiring any configured admin/dashboard role. `JUDGE` is an admin portal user with restricted downstream permissions.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/unit/auth/test_security.py tests/unit/auth/test_sessions.py tests/unit/auth/test_policies.py -v`

Expected: PASS, including expired access JWT rejection, old-refresh rejection after rotation, portal mismatch rejection, and 14-day idle expiry.

### Task 4: Authentication HTTP API, reset/invite email outbox, and rate limits

**Files:**
- Create: `src/api/dependencies.py`
- Create: `src/api/v1/auth.py`
- Create: `src/communications/outbox.py`
- Create: `src/communications/providers/email.py`
- Create: `src/communications/providers/sink.py`
- Create: `src/communications/providers/resend.py`
- Create: `src/audit/service.py`
- Test: `tests/integration/test_auth_api.py`
- Test: `tests/integration/test_password_reset.py`
- Test: `tests/integration/test_admin_invites.py`

**Interfaces:**
- Consumes Task 3 auth services and Task 2 persistence.
- Produces the `/api/v1/auth/*` contract and the email outbox rows consumed by Resend/local sink.

- [ ] **Step 1: Write failing API tests**

```python
def test_admin_login_returns_server_tokens_only_for_admin(client, super_admin):
    response = client.post("/api/v1/auth/login", json={
        "email": super_admin.email_normalized,
        "password": "correct horse battery staple",
        "portal": "admin",
    })

    assert response.status_code == 200
    assert response.json()["user"]["email"] == super_admin.email_normalized
    assert response.json()["tokens"]["access_token"]
    assert response.json()["tokens"]["refresh_token"]


def test_participant_login_cannot_use_admin_portal(client, participant_user):
    response = client.post("/api/v1/auth/login", json={
        "email": participant_user.email_normalized,
        "password": "correct horse battery staple",
        "portal": "admin",
    })

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "portal_access_denied"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_auth_api.py tests/integration/test_password_reset.py tests/integration/test_admin_invites.py -v`

Expected: FAIL because auth routes do not exist.

- [ ] **Step 3: Implement routes and stable errors**

Implement:

```text
POST /api/v1/auth/login
POST /api/v1/auth/refresh
POST /api/v1/auth/logout
GET  /api/v1/auth/me
POST /api/v1/auth/password/forgot
POST /api/v1/auth/password/reset
POST /api/v1/admin/invites
POST /api/v1/admin/invites/{invite_id}/accept
```

Login returns raw tokens only to server-side BFF callers; CORS must not expose this API directly to arbitrary browser origins. Route schema validation rejects malformed payloads. Login/reset responses use the same account-existence-safe message. Rate-limit ledger behavior is exactly:

```text
login failure: 5 attempts per normalized email + IP / 15 minutes
reset/invite send: 3 requests per email / 1 hour
```

Password reset and invites create hashed single-use tokens, create a redacted outbox row, and use the local sink in development. A successful reset revokes every refresh session for the user. All successful login/logout/reset/invite actions append audit rows without tokens/passwords.

- [ ] **Step 4: Run API tests**

Run: `uv run pytest tests/integration/test_auth_api.py tests/integration/test_password_reset.py tests/integration/test_admin_invites.py -v`

Expected: PASS, including generic reset response, replay rejection, session revocation, rate-limit `429`, and invite role-grant policy.

### Task 5: Participant BFF and protected dashboard replacement

**Files:**
- Modify: `/Users/subham/Dev/hackm/frontend/src/app/api/auth/login/route.ts`
- Modify: `/Users/subham/Dev/hackm/frontend/src/app/api/auth/logout/route.ts`
- Create: `/Users/subham/Dev/hackm/frontend/src/app/api/auth/refresh/route.ts`
- Create: `/Users/subham/Dev/hackm/frontend/src/app/api/auth/me/route.ts`
- Modify: `/Users/subham/Dev/hackm/frontend/src/lib/leader-auth.ts`
- Modify: `/Users/subham/Dev/hackm/frontend/src/app/dashboard/layout.tsx`
- Modify: `/Users/subham/Dev/hackm/frontend/src/app/(auth)/login/page.tsx`
- Test: `/Users/subham/Dev/hackm/frontend/src/lib/leader-auth.test.mjs`

**Interfaces:**
- Consumes FastAPI login/refresh/me/logout API from Task 4 using server-only `BACKEND_URL`.
- Produces same-origin participant cookies `hackmatrix_participant_access` and `hackmatrix_participant_refresh`, plus server guard `requireParticipantSession()`.

- [ ] **Step 1: Write failing participant guard test**

```js
import assert from "node:assert/strict"
import test from "node:test"

import { hasParticipantRole } from "./leader-auth.ts"

test("participant role check rejects admin-only claims", () => {
  assert.equal(hasParticipantRole(["PARTICIPANT"]), true)
  assert.equal(hasParticipantRole(["SUPER_ADMIN"]), false)
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/subham/Dev/hackm/frontend && node --test src/lib/leader-auth.test.mjs`

Expected: FAIL because the role-aware function does not exist.

- [ ] **Step 3: Implement BFF routes and guard**

In `login/route.ts`, forward the payload with fixed `portal: "participant"` to FastAPI. Do not accept portal from browser JSON. On success, write the two host-only HTTP-only cookies through Next.js `cookies()`/`NextResponse`; tokens never appear in the browser response body.

```ts
response.cookies.set("hackmatrix_participant_access", tokens.access_token, {
  httpOnly: true,
  sameSite: "lax",
  secure: process.env.NODE_ENV === "production",
  maxAge: 15 * 60,
  path: "/",
})
```

Dashboard layout reads the access cookie server-side, calls BFF/FastAPI `me`, redirects missing/expired/non-participant users to `/login`, and never uses the old deterministic HMAC credential path. Logout forwards refresh token server-side, clears both cookies even if the backend already considers the token invalid, then returns `{ ok: true }`.

- [ ] **Step 4: Run targeted participant checks**

Run: `cd /Users/subham/Dev/hackm/frontend && node --test src/lib/leader-auth.test.mjs && npm run lint && npx tsc --noEmit`

Expected: PASS. Document unrelated pre-existing failures rather than changing unrelated frontend code.

### Task 6: Admin BFF, login page, server guard, and live identity

**Files:**
- Create: `/Users/subham/Dev/admin-hackm/src/app/(auth)/login/page.tsx`
- Create: `/Users/subham/Dev/admin-hackm/src/app/api/auth/login/route.ts`
- Create: `/Users/subham/Dev/admin-hackm/src/app/api/auth/refresh/route.ts`
- Create: `/Users/subham/Dev/admin-hackm/src/app/api/auth/me/route.ts`
- Create: `/Users/subham/Dev/admin-hackm/src/app/api/auth/logout/route.ts`
- Create: `/Users/subham/Dev/admin-hackm/src/lib/auth.ts`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/(admin)/dashboard/layout.tsx`
- Modify: `/Users/subham/Dev/admin-hackm/src/components/admin-sidebar.tsx`

**Interfaces:**
- Consumes the Task 4 API with fixed `portal: "admin"`.
- Produces host-only admin cookies, `/login`, a guarded dashboard layout, and a functional logout menu.

- [ ] **Step 1: Write failing server auth helper test**

```ts
import { describe, expect, it } from "vitest"

import { isAdminRole } from "@/lib/auth"

describe("isAdminRole", () => {
  it("rejects participant-only claims", () => {
    expect(isAdminRole(["PARTICIPANT"])).toBe(false)
    expect(isAdminRole(["SUPER_ADMIN"])).toBe(true)
  })
})
```

- [ ] **Step 2: Add the existing test runner or use Node's test runner, then verify red**

Run: `cd /Users/subham/Dev/admin-hackm && npx vitest run src/lib/auth.test.ts`

Expected: FAIL because `auth.ts` and `vitest` are absent.

- [ ] **Step 3: Implement the smallest admin auth integration**

Use the same BFF pattern with cookie names `hackmatrix_admin_access` and `hackmatrix_admin_refresh`. The layout must call the server-side `me` check and redirect unauthenticated or participant-only users to `/login`. Replace the hardcoded `Subham` sidebar value with the fetched admin display name. Wire the existing Logout menu item to `POST /api/auth/logout`, clear cookies, and redirect to `/login`.

Use a native form with email/password, pending state, `role="alert"` for errors, and generic invalid-credential copy. Preserve the current dashboard visual system; do not redesign it.

- [ ] **Step 4: Run targeted admin checks**

Run: `cd /Users/subham/Dev/admin-hackm && npm run lint && npx tsc --noEmit && npx vitest run src/lib/auth.test.ts`

Expected: PASS. If adding Vitest is disproportionate to one helper, replace this one test with `node:test` and do not add Vitest.

### Task 7: End-to-end verification and documentation reconciliation

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`
- Modify: `docs/PRD.md`
- Test: `tests/e2e/test_auth_portals.py`
- Create: `docs/auth-phase-1-verification.md`

**Interfaces:**
- Consumes working FastAPI API and BFFs from Tasks 1–6.
- Produces reproducible development setup and recorded evidence for the Phase 1 acceptance criteria.

- [ ] **Step 1: Write a failing end-to-end auth test**

```python
def test_portal_isolation(auth_client, participant, super_admin):
    participant_login = auth_client.login(participant.email_normalized, "correct horse battery staple", "participant")
    admin_login = auth_client.login(super_admin.email_normalized, "correct horse battery staple", "admin")

    assert participant_login.status_code == 200
    assert admin_login.status_code == 200
    assert auth_client.login(participant.email_normalized, "correct horse battery staple", "admin").status_code == 403
    assert auth_client.login(super_admin.email_normalized, "correct horse battery staple", "participant").status_code == 403
```

- [ ] **Step 2: Run to verify red before the full flow is connected**

Run: `uv run pytest tests/e2e/test_auth_portals.py -v`

Expected: FAIL until all auth dependencies, migrations, and role policy are wired.

- [ ] **Step 3: Verify the full flow**

Run:

```bash
uv run alembic upgrade head
uv run python scripts/create_admin.py --email "$INITIAL_SUPER_ADMIN_EMAIL"
uv run pytest tests/unit/auth tests/integration/test_auth_api.py tests/integration/test_password_reset.py tests/e2e/test_auth_portals.py -v
```

Start FastAPI plus both Next dev servers using ignored local environment files. Use `agent-browser` with separate sessions to verify:

```text
participant: /dashboard → /login → valid login → /dashboard → reload → logout → /login
admin:       /dashboard → /login → valid login → /dashboard → reload → logout → /login
cross-role:  participant cannot enter admin; super admin cannot use participant portal
```

Record only URLs, status codes, and visible success/failure states. Do not record credentials, JWTs, refresh tokens, cookie values, or secret environment variables.

- [ ] **Step 4: Update setup documentation and inspect diffs**

Add `.env.example` variable names only; ensure `.env`, `.env.*`, private keys, generated mail-sink output, and browser state files are ignored. Update README with migration, bootstrap, test, and local-server commands using variable names rather than secret values.

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only planned files changed.

## Plan self-review

- **Spec coverage:** Phase 1 covers the approved Argon2id + EdDSA JWT + PostgreSQL refresh-session model, admin bootstrap/invites, password reset, Resend/local sink, rate limits, audit trail, separate protected participant/admin portal integration, Neon migrations, and end-to-end verification. Razorpay, registration drafts, activation continuation, payment adjustment, and ACM review are intentionally Phase 2 requirements.
- **Deliberate gap:** event configuration, payment, registration, submissions, dashboard fixtures, judging, and public results are not Phase 1; they already have named later phases in `docs/PRD.md`.
- **Type consistency:** API returns `IssuedTokens(access_token, refresh_token)`, BFF stores them only in HTTP-only cookies, `require_portal_access()` receives a user role collection plus a literal portal, and all session functions receive a SQLAlchemy `Session` and UTC `datetime`.
- **Placeholder scan:** no task relies on an undefined future service; every named function is produced by a prior task or defined in its own task.
