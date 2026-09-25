# HackMatrix backend

FastAPI and PostgreSQL API for HackMatrix 2026. It owns identity, registration pricing, Razorpay payment confirmation, leader activation, participant submissions, and the admin read models. The participant app (`hackm/frontend`) and admin app (`admin-hackm`) are separate clients.

PostgreSQL is the source of truth. A browser checkout callback is not proof of payment.

## Stack

- Python 3.13, `uv`, FastAPI, Uvicorn
- SQLAlchemy 2 and Alembic
- Neon PostgreSQL locally; the same migrations run on Docker PostgreSQL
- Argon2id passwords
- Ed25519 access JWTs, 15 minutes, plus hashed rotating refresh sessions
- Razorpay orders, signature checks, and deduplicated webhooks
- Resend in production, local email sink when Resend is not configured

## Layout

```text
src/api/            HTTP routes
src/auth/           passwords, JWTs, sessions, roles, rate limits
src/db/             SQLAlchemy models and session
src/payments/       Razorpay adapter
src/communications/ email templates and providers
src/registrations/  server-side pricing
migrations/         Alembic revisions
scripts/            operator commands
tests/              unit and integration tests
docs/               product decisions and plans
```

`docs/folder-structure.md` is a target layout. The running code is flatter than that document.

## Setup

```sh
uv sync
cp .env.example .env
```

Required environment values live in `.env` and are never committed:

- `NEON_DATABASE_URL` or `DATABASE_URL`
- `JWT_PRIVATE_KEY_PEM` and `JWT_PUBLIC_KEY_PEM`
- `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, and `RAZORPAY_WEBHOOK_SECRET`
- `RESEND_API_KEY` and `EMAIL_FROM` for live email
- `PARTICIPANT_APP_URL` for activation and reset links

Apply migrations:

```sh
uv run alembic upgrade head
```

Create the first super admin. The password is read from the prompt, not the command line.

```sh
uv run python scripts/create_admin.py --email you@hackmatrix.codes
```

## Run

```sh
uv run uvicorn src.main:app --host 127.0.0.1 --port 8000
```

Health check: `GET /healthz`.

## Product flow

1. A leader submits a team. The server calculates the quote: ₹200 for an Adamas University ACM member, ₹300 for any other ACM member, otherwise ₹400. A client-supplied amount is ignored.
2. The API creates a Razorpay order and returns only the public key, order ID, amount, currency, and team name.
3. Checkout confirmation verifies the Razorpay signature and captured status. Webhooks verify the raw body and ignore duplicate event IDs.
4. After capture, the registered leader receives a single-use activation link that expires in 30 minutes. The checkout browser is not given that token.
5. Activation sets the password and opens a participant session.
6. The leader can save a submission draft and finalize it. A finalized submission cannot be edited.
7. Admins can list registrations, teams, submissions, evaluations, and audit logs. Judges record rubric scores only after submission.

Forgot-password sends a reset link for an active account and a fresh activation link for a pending leader. Unknown emails get the same accepted response.

## API

Auth, prefix `/api/v1/auth`:

- `POST /login`, `/refresh`, `/logout`
- `GET /me`
- `GET /activation`, `POST /activate`
- `POST /password/forgot`, `POST /password/reset`
- `POST /admin/invites`, `POST /admin/invites/accept`

Registration and payments:

- `POST /api/v1/registrations`
- `POST /api/v1/payments/razorpay/confirm`
- `POST /api/v1/payments/razorpay/webhook`

Participant, prefix `/api/v1/participant`, leader access token required:

- `GET /dashboard`
- `PUT /submission`
- `POST /submission/finalize`

Admin, prefix `/api/v1/admin`, admin role required:

- `GET /registrations`
- `GET /teams`
- `GET /submissions`
- `POST /submissions/{registration_id}/assign`
- `GET /evaluations`
- `POST /evaluations`
- `GET /logs`

## Deploy

The image is `ghcr.io/subham12r/hackm-backend`. A push to `master` builds it and restarts the container on the VPS. Host ports `8000`, `8080`, `8082`, and `8091` are already taken, so nginx reaches this API at `127.0.0.1:8092`.

HackMatrix Postgres runs as its own container, `hackm-postgres`, with no host port. SecureMail keeps `127.0.0.1:5432`. In `/opt/apps/hackm-backend/.env`, set `POSTGRES_PASSWORD` and `DATABASE_URL=postgresql+psycopg://hackm:<same-password>@db:5432/hackm`. Store PEM values with literal `\n`. Then install `deploy/nginx/hackmatrix-api.conf` and reload nginx.

## Tests

```sh
uv run pytest
```

Integration tests expect PostgreSQL at `TEST_DATABASE_URL`, defaulting to `postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test`.

## Further reading

- `CONTEXT.md` for domain terms and invariants
- `docs/PRD.md` for the product contract
