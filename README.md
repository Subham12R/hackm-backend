# HackMatrix backend

FastAPI and PostgreSQL API behind HackMatrix 2026. It owns identity, registration pricing, Razorpay payment
confirmation, leader activation, events and submissions, judging, certificates, and every admin read model.

Two apps use it today, and yours can too:

- **`hackm`**, the participant site (Next.js, `frontend/`)
- **`admin-hackm`**, the ORG. admin panel for organisers and judges (Next.js)

PostgreSQL is the source of truth. A browser checkout callback is not proof of payment.

## API documentation

| | |
|---|---|
| **[Developer guide](docs/api/README.md)** | Auth and roles, error format, conventions, the main flows with code, rate limits, troubleshooting |
| **[OpenAPI spec](docs/api/openapi.json)** | Every operation with request and response schemas and examples. Import it into Postman or Insomnia, or generate a typed client with `openapi-typescript` |
| **[Endpoint index](docs/api/endpoints.md)** | One table per area: method, path, roles, body, success status |
| **Live reference** | `/docs` (Swagger UI) and `/redoc` on any running API, e.g. <https://api.hackmatrix.codes/docs> |

The spec is generated from the code and checked by a test, so it can't drift silently. After changing a route, run
`uv run python scripts/export_openapi.py` and commit the result (details in the guide's
[last section](docs/api/README.md#keeping-the-docs-current)).

Integrating a frontend in one paragraph: call the API from your server, not the browser (there is no CORS, and
tokens stay in httpOnly cookies). Sign in with `POST /api/v1/auth/login {email, password, portal}`, send
`Authorization: Bearer <access_token>`, and on a `401` refresh once with `POST /api/v1/auth/refresh`, sharing one
refresh between parallel requests because refresh tokens are single use. Errors are `{"detail": "..."}`. The guide has
a copy-paste Next.js proxy.

## Stack

- Python 3.13, `uv`, FastAPI, Uvicorn
- SQLAlchemy 2 and Alembic on PostgreSQL 16 (Neon or Docker)
- Argon2id passwords
- Ed25519 access JWTs, 15 minutes, plus hashed rotating refresh sessions
- Razorpay orders, signature checks, and deduplicated webhooks
- Resend for email in production, a local sink when it isn't configured
- Optional: Redis (cache and live notifications across workers), S3 or Cloudflare R2 (gallery, certificates, uploads)

## Layout

```text
src/main.py         app factory: routers, error handler, background workers
src/openapi.py      the OpenAPI document (schemas, roles, recorded examples)
src/api/            HTTP routes, one module per area (auth, participant, judge, admin_*)
src/auth/           passwords, JWTs, sessions, roles and policies, rate limits
src/db/             SQLAlchemy models and session
src/events/         stages (events), windows, problem statements, finals
src/judging/        rubric scoring
src/credentials/    certificates and badges
src/payments/       Razorpay adapter
src/communications/ email templates, providers, outbox
src/registrations/  server-side pricing, roster rules, unpaid sweep
src/storage/        S3 / R2 presigned uploads
migrations/         Alembic revisions
scripts/            operator commands (create_admin, export_openapi)
tests/              unit and integration tests
docs/               API docs, product decisions and plans
```

## Local setup

You need Python 3.13 with [`uv`](https://docs.astral.sh/uv/) and a PostgreSQL 16 database.

```sh
uv sync
cp .env.example .env

# A throwaway Postgres, if you don't have one
docker run -d --name hackm-dev-db -p 5433:5432 \
  -e POSTGRES_USER=hackm -e POSTGRES_PASSWORD=hackm -e POSTGRES_DB=hackm postgres:16-alpine
echo 'DATABASE_URL=postgresql+psycopg://hackm:hackm@127.0.0.1:5433/hackm' >> .env

# Signing keys: without them the auth, participant, judge and admin routes are not mounted
uv run python - >> .env <<'PY'
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization as s
k = Ed25519PrivateKey.generate()
pem = lambda b: b.decode().strip().replace("\n", "\\n")
print("JWT_PRIVATE_KEY_PEM=" + pem(k.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption())))
print("JWT_PUBLIC_KEY_PEM=" + pem(k.public_key().public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo)))
PY

uv run alembic upgrade head
uv run python scripts/create_admin.py --email you@example.com   # prompts for the password
uv run uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload
```

Open <http://127.0.0.1:8000/docs>, call `POST /api/v1/auth/login` with `portal: "admin"`, and paste the
`access_token` into **Authorize**. `GET /healthz` answers `{"status":"ok"}` once the database is reachable.

Then point a client at it: `BACKEND_URL=http://127.0.0.1:8000` in `admin-hackm` or `hackm/frontend`.

## Environment

Values live in `.env` (never committed). Only the database and the signing keys are needed to boot; everything else
switches a feature on.

| Variable | Needed for | Notes |
|---|---|---|
| `DATABASE_URL` or `NEON_DATABASE_URL` | Everything | `postgresql+psycopg://user:pass@host:port/db` |
| `JWT_PRIVATE_KEY_PEM`, `JWT_PUBLIC_KEY_PEM` | Every signed-in route | Ed25519 PEM with literal `\n` for newlines. Rotating them signs everyone out |
| `PARTICIPANT_APP_URL`, `ADMIN_APP_URL` | Links in emails | Activation, reset, invite and verify links |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Paid registration | Test keys locally. Without them registration answers `503` on paid events |
| `RAZORPAY_WEBHOOK_SECRET` | Payment webhooks | Enable `payment.captured`, `payment.failed`, `order.paid`, `refund.processed`, `refund.failed` |
| `RAZORPAY_FEE_PERCENT`, `RAZORPAY_FEE_GST_PERCENT` | Fee pass-through | Default `2` and `18`: Razorpay's cut is added on top of the tier price. `0` turns it off |
| `RESEND_API_KEY` | Sending email | Without it, mail goes to a local sink (logged) |
| `EMAIL_FROM_NOREPLY`, `EMAIL_FROM_SUPPORT`, `EMAIL_FROM_ANNOUNCEMENTS`, `EMAIL_DOMAIN`, `EMAIL_BANNER_URL` | Email branding | See [Email senders](#email-senders) |
| `OWNER_EMAIL`, `OWNER_INBOX` | Owner account | The protected owner login, and where its mail is delivered |
| `REDIS_URL` | Cache, live notifications across workers | Optional with a single worker |
| `R2_*` or `S3_*` plus `AWS_*` | Gallery, certificates, email attachments | See [Photo gallery storage](#photo-gallery-storage). Without a bucket, uploads answer `503` |
| `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_CACHE_SECONDS` | Home page Instagram feed | See [Instagram feed](#instagram-feed) |
| `CREDENTIAL_ISSUER` | Certificates | Issuer line, default "ACM Student Chapter, Adamas University" |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | Tuning | Per API process |
| `EMAIL_OUTBOX_WORKER`, `UNPAID_REGISTRATION_WORKER`, `CREDENTIAL_RENDER_WORKER` | Background loops | `1` by default; set `0` on all but one process when running several |
| `NOTIFY_HEARTBEAT_SECONDS` | Live notifications | Default 20 |

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

The full reference moved to [`docs/api/`](docs/api/README.md). Areas, by prefix:

- `/api/v1/public/*`: event, leaderboard, gallery, site notices, Instagram, credential verification, contact form
- `/api/v1/registrations*` and `/api/v1/payments/razorpay/*`: team registration, invites, RSVP, payment
- `/api/v1/auth/*`: login, refresh, logout, me, activation, password reset and change, staff invites
- `/api/v1/participant/*`: the team leader's dashboard, roster, events, submissions, results, live stream
- `/api/v1/judge/*`: a judge's assignments and scorecards
- `/api/v1/admin/*`: everything organisers do in the ORG. admin panel

Teams register and pay once; each event ("stage" in the code) has its own submission window, problem statements
("tracks"), submissions, judging rounds and winners. Migration `0012_stages` moved the original single schedule,
challenge, submissions and rounds into a published "Main round", and the older event-level routes
(`/participant/submission`, `/admin/challenge`) act on the first event.

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

## Instagram feed

The home page shows the latest posts and reels from @hackmatrix_2k26 through `GET /api/v1/public/instagram`. It reads the Instagram API with Instagram Login, caches the result for 10 minutes (`INSTAGRAM_CACHE_SECONDS`), and serves the last good copy if Instagram is down. Without a token the endpoint answers `{"configured": false, ...}` and the site shows only the profile header and a Follow button.

1. In the Instagram app, switch @hackmatrix_2k26 to a professional account (Creator or Business): Settings → Account type and tools → Switch to professional account. It is free and needs no Facebook Page.
2. At developers.facebook.com, create an app, pick the "Manage messaging & content on Instagram" use case, and choose the Business app type.
3. In the app, open Instagram → API setup with Instagram login → Generate access tokens → Add account, sign in as @hackmatrix_2k26, and allow `instagram_business_basic`. Copy the token it shows. The app can stay in Development mode because it only reads its own account.
4. Set `INSTAGRAM_ACCESS_TOKEN` in the server `.env` and restart the container.

The token lasts 60 days. The API refreshes it every 7 days (the first refresh runs once the token is a day old) and keeps the fresh copy in `instagram_tokens`, so the env var does not need rotating. To replace it, paste a new token into `.env`; a changed env token wins over the stored one.

## Deploy

The image is `ghcr.io/subham12r/hackm-backend`. A push to `master` builds it and restarts the container on the VPS. Host ports `8000`, `8080`, `8082`, and `8091` are already taken, so nginx reaches this API at `127.0.0.1:8092`.

HackMatrix Postgres runs as its own container, `hackm-postgres`, with no host port. SecureMail keeps `127.0.0.1:5432`. In `/opt/apps/hackm-backend/.env`, set `POSTGRES_PASSWORD` and `DATABASE_URL=postgresql+psycopg://hackm:<same-password>@db:5432/hackm`. Store PEM values with literal `\n`. Then install `deploy/nginx/hackmatrix-api.conf` and reload nginx.

## Tests

```sh
docker run -d --name hackm-test-db -p 55432:5432 \
  -e POSTGRES_USER=hackm_test -e POSTGRES_PASSWORD=hackm_test -e POSTGRES_DB=hackm_test postgres:16-alpine
uv run pytest
```

Integration tests expect PostgreSQL at `TEST_DATABASE_URL`, defaulting to
`postgresql+psycopg://hackm_test:hackm_test@127.0.0.1:55432/hackm_test`. They run migrations themselves.

`tests/unit/test_openapi_docs.py` checks that `docs/api/` matches the code. To refresh the recorded response examples
as well, run `uv run python scripts/export_openapi.py --record` (it reruns the suite).

The production image installs with `uv sync --no-dev`, so anything imported at runtime must be a main dependency, not
a dev one. Tests won't catch that; `uv sync --frozen --no-dev` in a clean environment plus `python -c "import
src.main"` will.

## Further reading

- [`docs/api/README.md`](docs/api/README.md) for building on the API, including troubleshooting
- `CONTEXT.md` for domain terms and invariants
- `docs/PRD.md` for the product contract
