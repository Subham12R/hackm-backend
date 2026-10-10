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
- `RESEND_API_KEY` for live email (senders below)
- `OWNER_EMAIL` (optional, default `admin@hackmatrix.codes`): the protected owner account
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
- `GET /stages` lists the events the team takes part in, their problem statements once revealed, and the team's submission for each
- `PUT /stages/{stage_id}/submission` and `POST /stages/{stage_id}/submission/finalize` (send `track_id` to pick a problem statement)
- `PUT /submission`, `POST /submission/finalize` and `GET /challenge` act on the team's current event, for older clients

Admin, prefix `/api/v1/admin`, admin role required:

- `GET /registrations`
- `GET /teams`
- `GET /stages`, `POST /stages`, `PATCH /stages/{id}`, `DELETE /stages/{id}` manage events (online round, finals, ...): schedule, who takes part (`ALL_PAID` or `FINALISTS`), published
- `POST /stages/{id}/tracks`, `PUT /tracks/{id}`, `DELETE /tracks/{id}` manage an event's problem statements
- `GET /submissions?stage_id=&track_id=` lists one event's submissions (the first event when left out)
- `POST /submissions/{registration_id}/assign?stage_id=`
- `POST /submissions/{submission_id}/awards` (`placement`, optional `label` and `track_id`) and `DELETE /awards/{id}` mark winners per event and per track
- `GET /evaluations`
- `POST /evaluations`
- `GET /logs`
- `POST /teams/{registration_id}/finals` accepts or declines a paid team for the finals
- `GET /checkins` lists finals teams with members, arrival counts, and a 15-minute arrival timeline
- `POST /checkins/members/{member_id}` and `POST /checkins/teams/{registration_id}` mark or undo arrivals
- `GET /gallery`, `POST /gallery/uploads` (presigned S3 POST), `POST /gallery`, `PATCH /gallery/{id}`, `DELETE /gallery/{id}`

Teams register and pay once; each event ("stage" in the code) has its own submission window, problem statements, submissions, judging rounds and winners. Migration `0012_stages` moves the existing schedule, challenge, submissions and rounds into a published "Main round". The event-level submission times and `/admin/challenge` now act on the first event.

Public, prefix `/api/v1/public`: `GET /event`, `GET /leaderboard`, `GET /gallery`, `POST /contact`.

## Live notifications

Participant dashboards hold one Server-Sent Events connection (`GET /api/v1/participant/notifications/stream`). Endpoints
publish after they commit: announcements and released results go to every team; finals decisions, check-ins, organiser
track changes and reopened submissions go to that team only. The dashboard shows each one as a toast and refetches.

- `REDIS_URL` carries messages between API workers and containers (Redis pub/sub on `hm:notify:*`). Without it delivery
  is in-process, which is only correct with a single worker.
- `NOTIFY_HEARTBEAT_SECONDS` (default 20) keeps idle connections open through nginx and Cloudflare.
- Both nginx configs have a dedicated `location` for the stream with buffering off and a one-hour read timeout.

## Email senders

Every email goes out from one of three mailboxes on `hackmatrix.codes`:

| Sender | Used for | Reply-To |
|---|---|---|
| `noreply@` | Activation, payments, password resets, invites, finals decisions, owner security codes | `support@` |
| `support@` | Replies to contact-form messages | itself |
| `announcements@` | Published announcements, and staff emails by default (the Email page can pick any sender) | `support@` |

Only `RESEND_API_KEY` is required: verifying the domain in Resend covers all three addresses. Optional overrides take a full
`Name <address>` value: `EMAIL_FROM_NOREPLY` (`EMAIL_FROM` still works as its old name), `EMAIL_FROM_SUPPORT`,
`EMAIL_FROM_ANNOUNCEMENTS`, or `EMAIL_DOMAIN` to move all three to another domain.

DNS (Cloudflare) for `hackmatrix.codes`, all records DNS-only:

1. Resend → Domains → add `hackmatrix.codes` and create the records it lists (DKIM `resend._domainkey` TXT, and the `send`
   subdomain's SPF TXT and MX). Wait for "Verified".
2. `_dmarc` TXT: `v=DMARC1; p=none; rua=mailto:support@hackmatrix.codes` (tighten to `p=quarantine` once reports look clean).
3. Email Routing → enable it, then forward `support@` and `announcements@` to the inbox the team reads. It adds the root MX
   and SPF records itself; they do not clash with Resend, which sends from the `send` subdomain. Leave `noreply@` unrouted
   (or route it to a drop rule); its Reply-To already points people at `support@`.

## Photo gallery storage

Gallery photos live in S3. Admins' browsers upload straight to the bucket with a presigned POST that S3 itself limits to 15 MB and JPEG, PNG, WebP or AVIF; the API then confirms the object exists before listing it.

1. Create a bucket, for example `hackmatrix-gallery` in `ap-south-1`.
2. Make `gallery/*` publicly readable (bucket policy `s3:GetObject` on `arn:aws:s3:::hackmatrix-gallery/gallery/*`, with "Block public access" relaxed for bucket policies), or put CloudFront in front and set `S3_PUBLIC_BASE_URL`.
3. Add a CORS rule so the admin app can POST: allowed origin the admin URL, method `POST`, header `*`.
4. Create an IAM user or role limited to `s3:PutObject`, `s3:GetObject`, and `s3:DeleteObject` on `arn:aws:s3:::hackmatrix-gallery/gallery/*`.
5. Set `S3_BUCKET`, `S3_REGION`, `AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY` in the server `.env`.

### Cloudflare R2

R2 has no POST uploads, so for R2 (or any `S3_ENDPOINT_URL`) the API signs a PUT instead (content type and length are part of the signature, and the API still checks the object before listing it).

1. Create an R2 bucket, for example `hackmatrix-gallery`.
2. Make it publicly readable: enable the `r2.dev` URL or connect a custom domain under the bucket's Settings → Public access.
3. Add a CORS policy: allowed origin the admin URL, methods `PUT` and `GET`, allowed headers `Content-Type` and `Cache-Control`.
4. Create an R2 API token with "Object Read & Write" scoped to that bucket.
5. Set in the server `.env`: `R2_BUCKET`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (the token's keys) and `R2_PUBLIC_URL` (the r2.dev URL or custom domain). `R2_BUCKET` takes precedence over the `S3_*` settings.

Without `S3_BUCKET` the API still runs; the public gallery is empty and uploads answer 503.

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
