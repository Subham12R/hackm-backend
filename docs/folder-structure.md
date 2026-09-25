# HackMatrix backend folder structure

This structure is for `hackm-backend` only. `hackm` and `admin-hackm` are reference clients and remain separate repositories; they are not repeated here.

The backend is organized by domain and API boundary, with PostgreSQL migrations and provider adapters kept explicit. It avoids a generic repository/factory layer until a second implementation genuinely needs one.

```text
hackm-backend/
├── src/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── lifecycle.py
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── errors.py
│   │   ├── dependencies.py
│   │   ├── middleware.py
│   │   └── v1/
│   │   ├── __init__.py
│   │   ├── router.py
│   │   ├── health.py
│   │   ├── public.py
│   │   ├── auth.py
│   │   ├── registrations.py
│   │   ├── payments.py
│   │   ├── participant.py
│   │   ├── webhooks.py
│   │   └── admin/
│   │       ├── __init__.py
│   │       ├── overview.py
│   │       ├── registrations.py
│   │       ├── teams.py
│   │       ├── submissions.py
│   │       ├── evaluations.py
│   │       ├── leaderboard.py
│   │       ├── challenge.py
│   │       ├── contacts.py
│   │       ├── announcements.py
│   │       ├── analytics.py
│   │       ├── event_control.py
│   │       ├── audit_logs.py
│   │       └── settings.py
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── session.py
│   │   ├── transaction.py
│   │   └── models/
│   │   ├── __init__.py
│   │   ├── identity.py
│   │   ├── event.py
│   │   ├── registration.py
│   │   ├── team.py
│   │   ├── payment.py
│   │   ├── submission.py
│   │   ├── evaluation.py
│   │   ├── communication.py
│   │   └── audit.py
│   │
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── schemas.py
│   │   ├── service.py
│   │   ├── security.py
│   │   ├── sessions.py
│   │   ├── policies.py
│   │   └── rate_limits.py
│   │
│   ├── events/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   └── policies.py
│   │
│   ├── registrations/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   ├── pricing.py
│   │   ├── validation.py
│   │   └── policies.py
│   │
│   ├── teams/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   └── policies.py
│   │
│   ├── payments/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   ├── state.py
│   │   ├── idempotency.py
│   │   ├── ports.py
│   │   └── providers/
│   │   ├── __init__.py
│   │   ├── razorpay.py
│   │   └── fake.py
│   │
│   ├── activation/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   └── tokens.py
│   │
│   ├── submissions/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   ├── versions.py
│   │   └── policies.py
│   │
│   ├── evaluations/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   ├── projections.py
│   │   ├── scoring.py
│   │   └── policies.py
│   │
│   ├── leaderboard/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   ├── calculation.py
│   │   └── policies.py
│   │
│   ├── communications/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── templates.py
│   │   ├── outbox.py
│   │   └── providers/
│   │   ├── __init__.py
│   │   ├── email.py
│   │   └── sink.py
│   │
│   ├── contacts/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   └── policies.py
│   │
│   ├── announcements/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   └── schemas.py
│   │
│   ├── audit/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   └── redaction.py
│   │
│   ├── projections/
│   │   ├── __init__.py
│   │   ├── participant.py
│   │   ├── admin.py
│   │   ├── judge.py
│   │   └── public.py
│   │
│   └── observability/
│       ├── __init__.py
│       ├── logging.py
│       ├── metrics.py
│       └── request_ids.py
│
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│   ├── <timestamp>_identity_and_sessions.py
│   ├── <timestamp>_events_and_registrations.py
│   ├── <timestamp>_payments.py
│   ├── <timestamp>_submissions_and_evaluations.py
│   └── <timestamp>_communications_and_audit.py
│
├── scripts/
│   ├── seed_dev.py
│   ├── create_admin.py
│   ├── replay_webhook.py
│   └── check_migrations.py
│
├── tests/
│   ├── unit/
│   │   ├── auth/
│   │   ├── registrations/
│   │   ├── payments/
│   │   ├── submissions/
│   │   ├── evaluations/
│   │   └── leaderboard/
│   ├── integration/
│   │   ├── conftest.py
│   │   ├── test_migrations.py
│   │   ├── test_registration_payment_flow.py
│   │   ├── test_auth_flow.py
│   │   ├── test_participant_projection.py
│   │   ├── test_admin_projection.py
│   │   ├── test_judge_blindness.py
│   │   └── test_seed.py
│   ├── contracts/
│   │   ├── test_payment_provider.py
│   │   └── test_email_provider.py
│   └── e2e/
│   ├── fixtures/
│   ├── test_registration_to_activation.py
│   └── test_dashboard_sync.py
│
├── docs/
│   ├── PRD.md
│   └── folder-structure.md
├── .env.example
├── .gitignore
├── alembic.ini
├── pyproject.toml
├── README.md
└── uv.lock
```

## Module ownership

### `api/`

Only HTTP concerns: routing, dependency wiring, request/response schemas, status codes, and error envelopes. API handlers call domain services; they do not contain pricing, password, payment, or state-transition rules.

### `db/`

SQLAlchemy engine/session lifecycle and mapped tables. Alembic owns schema history. Database constraints duplicate critical application invariants such as unique normalized emails, team names, provider IDs, webhook event IDs, assignments, and evaluation ownership.

### `auth/` and `activation/`

Account lifecycle, Argon2id password handling, opaque session tokens, role policies, activation links, password reset tokens, revocation, and rate limits. Neither frontend is allowed to reimplement these rules.

### `registrations/`, `teams/`, and `payments/`

The registration transaction creates the team/member snapshot and server quote. The payment service creates provider orders, verifies checkout results, handles signed webhooks, and changes registration fulfilment only after a verified captured/paid state. Provider-specific HTTP/auth/signature code stays under `payments/providers/`.

### `submissions/`, `evaluations/`, and `leaderboard/`

These modules own server state transitions, deadlines, anonymous-code generation, judge assignment, role projections, score calculation, result snapshots, and explicit release. A client callback or hidden UI control is never an authority.

### `communications/`, `contacts/`, and `announcements/`

Public contact/newsletter writes, activation/reset emails, admin conversations, announcements, and durable outbox/delivery state. The email provider is injected behind `communications/providers/email.py`; `sink.py` is the local development implementation.

### `projections/`

Explicit serializers/read models for participant, admin, judge, and public audiences. This is where sensitive fields are omitted by construction. Do not serialize an admin object and filter it in the frontend.

### `audit/` and `observability/`

Append-only business audit events are separate from operational logs. Both redact credentials, tokens, OTPs, provider secrets, and unnecessary PII. Request IDs connect HTTP errors, provider calls, outbox work, and audit rows without putting secrets in the ID.

## Runtime boundaries

```text
Next.js participant BFF ─┐
                         │ HTTPS/internal network
Next.js admin BFF ───────┤
                         ▼
                    api/v1/
                         │
                 domain services
                         │
              SQLAlchemy transaction
                         │
                   PostgreSQL

Payment provider ──signed webhook──> api/v1/payments/{provider}/webhook
Email provider  <──outbox worker──── communications/
```

- `DATABASE_URL` is the only database-provider boundary: Neon now, Docker PostgreSQL later.
- Browser clients never receive database URLs, password hashes, provider secret keys, or reset/activation token values in JavaScript.
- Frontends may use same-origin BFF routes for server-rendered guards, but the backend remains responsible for sessions and permissions.
- Webhooks acknowledge quickly after signature validation and durable event insertion; bounded background processing applies the state transition.
- No Redis is required for the first backend. PostgreSQL-backed outbox/idempotency/rate-limit primitives are the initial source of truth; Redis can be added only after measured need.

## Implementation order

1. `config`, `db`, `api/errors`, health, migrations, request IDs, and test fixtures.
2. Identity, roles, sessions, activation, password reset, and email sink.
3. Events, pricing rules, registrations, teams, payment port, provider fixture, and payment webhook state machine.
4. Razorpay adapter after provider confirmation; live test-mode verification before production credentials.
5. Participant/admin projections and dashboard endpoints.
6. Submissions, evaluations, leaderboard, contacts, announcements, analytics, settings, and audit views.
7. Neon migration rehearsal, backup/restore check, and Docker PostgreSQL deployment configuration.

Do not create empty modules for later features ahead of the first vertical slice. Add each domain's model, migration, service, API projection, and focused tests together.
