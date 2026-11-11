# HackMatrix API guide

Everything a frontend developer needs to build on `hackm-backend`: where the reference lives, how auth works, the
error format, the main flows with code, and a troubleshooting section.

The HackMatrix participant site (`hackm`) and the ORG. admin panel (`admin-hackm`) are both clients of this API and
follow the patterns below, so either one is a working example.

| What | Where |
|---|---|
| Interactive reference (try requests in the browser) | `/docs` (Swagger UI) or `/redoc` on any running API, e.g. `https://api.hackmatrix.codes/docs` |
| Machine-readable spec (OpenAPI 3.1) | [`openapi.json`](openapi.json) in this folder, or `GET /openapi.json` on a running API |
| One-page list of every operation with its roles | [`endpoints.md`](endpoints.md) |
| This guide | concepts, flows, examples, support |

The spec is generated from the code (see [Keeping the docs current](#keeping-the-docs-current)): request bodies and
parameters come from the route signatures, response schemas and examples are recorded from the integration tests,
and the roles on each route are read from the handler's access check. When the code changes, the spec changes with
it and a test fails until the committed copy is regenerated.

## Contents

1. [Quick start](#1-quick-start)
2. [Generate a typed client](#2-generate-a-typed-client)
3. [Call the API from a server, not the browser](#3-call-the-api-from-a-server-not-the-browser)
4. [Authentication and roles](#4-authentication-and-roles)
5. [Errors](#5-errors)
6. [Conventions](#6-conventions)
7. [Flows](#7-flows)
8. [Rate limits](#8-rate-limits)
9. [Support and troubleshooting](#9-support-and-troubleshooting)
10. [Keeping the docs current](#keeping-the-docs-current)

## 1. Quick start

```sh
API=https://api.hackmatrix.codes            # or http://127.0.0.1:8000 for a local uvicorn

curl -s $API/healthz                         # {"status":"ok"}
curl -s $API/api/v1/public/event             # schedule, registration window, prices
curl -s $API/api/v1/public/leaderboard       # released results, or {"released": false, ...}

# Sign in (portal is "participant" for team leaders, "admin" for staff and judges)
curl -s -X POST $API/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"…","portal":"admin"}'
# → {"tokens":{"access_token":"eyJ…","refresh_token":"…"},"user":{"id":"…","email":"…","display_name":"…","roles":["SUPER_ADMIN"],"is_owner":false}}

curl -s $API/api/v1/auth/me -H "Authorization: Bearer $ACCESS"
```

To run the API locally, follow the [repository README](../../README.md#local-setup).

## 2. Generate a typed client

The spec has real request and response schemas, so you can generate TypeScript types instead of writing them.

```sh
npm i openapi-fetch
npm i -D openapi-typescript
npx openapi-typescript https://api.hackmatrix.codes/openapi.json -o src/lib/hackmatrix-api.d.ts
# or from a checkout: npx openapi-typescript ../hackm-backend/docs/api/openapi.json -o …
```

```ts
// src/lib/hackmatrix.ts  (server-side only: see section 3)
import createClient from "openapi-fetch"
import type { paths } from "./hackmatrix-api"

export const hackmatrix = createClient<paths>({ baseUrl: process.env.BACKEND_URL })

const { data, error } = await hackmatrix.GET("/api/v1/public/event")
const login = await hackmatrix.POST("/api/v1/auth/login", {
  body: { email, password, portal: "participant" },
})
```

Postman and Insomnia can import `openapi.json` directly (Import → File or URL). Set a collection variable for the
bearer token; every signed-in operation already declares `bearerAuth`.

Response schemas are inferred from what the tests saw, so a field the tests never returned can be missing from the
schema. The example next to each schema is a real (scrubbed) response. When in doubt, the handler in `src/api/` is the
source of truth.

## 3. Call the API from a server, not the browser

The API sends no CORS headers, and that is deliberate: tokens must never reach browser JavaScript. Every client puts
a small server-side layer in front of it (a "BFF", backend for frontend):

```
browser ──same-origin fetch──▶ your server route ──Authorization: Bearer…──▶ hackm-backend
          httpOnly cookies         (Next.js route handler, Express, …)
```

What the BFF does, in the order the existing apps do it (`admin-hackm/src/lib/admin-backend.ts`,
`hackm/frontend/src/lib/participant-backend.ts`):

1. Keeps the access and refresh tokens in two `httpOnly`, `SameSite=Lax`, `Secure` cookies. Give the access cookie a
   long `maxAge` (the apps use 30 days) even though the JWT inside lasts 15 minutes; otherwise the cookie disappears
   before you get the chance to refresh it.
2. Refuses cross-site mutations: for anything but GET/HEAD, require `Sec-Fetch-Site: same-origin` or an `Origin`
   that matches the host.
3. Forwards the call with `Authorization: Bearer <access>` and `X-Client-IP: <browser IP>` (the login rate limit uses
   it, because every request otherwise comes from your server's IP).
4. On `401`, refreshes **once** and retries. Refresh tokens rotate on every use, so parallel requests must share one
   in-flight refresh per refresh token, or the second one presents a revoked token and signs the user out.
5. Writes the new tokens back to the cookies, and clears them when the refresh itself returns `401`.

A minimal Next.js (App Router) version:

```ts
// app/api/hm/[...path]/route.ts
import { cookies } from "next/headers"
import { NextResponse } from "next/server"

const BACKEND = process.env.BACKEND_URL!          // e.g. http://127.0.0.1:8000, never exposed to the browser
const inFlight = new Map<string, Promise<{ access: string; refresh: string } | null>>()

function refreshOnce(refresh: string) {
  let pending = inFlight.get(refresh)
  if (!pending) {
    pending = fetch(`${BACKEND}/api/v1/auth/refresh`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    })
      .then(async (r) => (r.ok ? (await r.json()).tokens : null))
      .then((t) => (t ? { access: t.access_token, refresh: t.refresh_token } : null))
      .finally(() => setTimeout(() => inFlight.delete(refresh), 10_000))
    inFlight.set(refresh, pending)
  }
  return pending
}

async function handler(request: Request, { params }: { params: Promise<{ path: string[] }> }) {
  const { path } = await params
  if (path.some((s) => !/^[A-Za-z0-9_-]+$/.test(s))) return NextResponse.json({ error: "Not found" }, { status: 404 })
  const jar = await cookies()
  let access = jar.get("hm_access")?.value
  const refresh = jar.get("hm_refresh")?.value
  const body = ["GET", "HEAD"].includes(request.method) ? undefined : await request.text()
  const search = new URL(request.url).search
  const call = (token?: string) =>
    fetch(`${BACKEND}/api/v1/${path.join("/")}${search}`, {
      method: request.method,
      headers: {
        "content-type": "application/json",
        "X-Client-IP": request.headers.get("x-forwarded-for")?.split(",")[0] ?? "",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body,
      cache: "no-store",
    })

  let upstream = await call(access)
  let rotated: { access: string; refresh: string } | null = null
  if (upstream.status === 401 && refresh) {
    rotated = await refreshOnce(refresh)
    if (rotated) upstream = await call((access = rotated.access))
  }
  const response = new NextResponse(upstream.body, { status: upstream.status, headers: upstream.headers })
  if (rotated) {
    const opts = { httpOnly: true, sameSite: "lax" as const, secure: true, path: "/", maxAge: 60 * 60 * 24 * 30 }
    response.cookies.set("hm_access", rotated.access, opts)
    response.cookies.set("hm_refresh", rotated.refresh, opts)
  }
  return response
}

export { handler as GET, handler as POST, handler as PUT, handler as PATCH, handler as DELETE }
```

Public routes (`/api/v1/public/*`, `/api/v1/registrations*`, the payment confirm) need no token, but they still have
no CORS, so call them from your server too (a server component, a route handler, or `getStaticProps`).

If you really need direct browser access for your own deployment, add `CORSMiddleware` in `src/main.py` with an
explicit origin list, and keep using a server for anything that holds a token.

## 4. Authentication and roles

### Tokens

| Token | Format | Lifetime | Notes |
|---|---|---|---|
| Access | Ed25519-signed JWT (`alg: EdDSA`, `iss: hackmatrix-api`, `aud: hackmatrix-bff`, `sub`: user id) | 15 minutes | Send as `Authorization: Bearer …`. Don't parse it for roles; call `/auth/me`. |
| Refresh | Opaque random string, stored hashed | Long-lived, single use | `POST /auth/refresh` returns a new pair and revokes the old refresh token. Reusing an old one returns `401 invalid refresh token`. |

| Endpoint | Body | Returns |
|---|---|---|
| `POST /api/v1/auth/login` | `{email, password, portal}` | `{tokens, user}` |
| `POST /api/v1/auth/refresh` | `{refresh_token}` | `{tokens}` |
| `POST /api/v1/auth/logout` | `{refresh_token}` | `204`; revokes that refresh token |
| `GET /api/v1/auth/me` | | `{user: {id, email, display_name, roles, is_owner}}` |
| `POST /api/v1/auth/password/change` | `{current_password, new_password}` | `204` |

`portal` picks which door the user is coming through. `"participant"` accepts only accounts with the `PARTICIPANT`
role (team leaders). `"admin"` accepts any account with a staff role, judges included. A correct password through the
wrong portal is still `401 invalid credentials`.

### Roles

| Role | Can do |
|---|---|
| `SUPER_ADMIN` | Everything, including money (payments, refunds), staff management and the owner tools |
| `EVENT_ADMIN` | Events, problem statements, announcements, judging setup, results, check-in, emails |
| `REGISTRATION_ADMIN` | Registrations and team review, check-in |
| `EVALUATION_ADMIN` | Judging assignments and rubrics |
| `SUPPORT_ADMIN` | Contact inbox, emails, check-in |
| `VIEWER` | Read-only staff access |
| `JUDGE` | Only `/api/v1/judge/*`: assigned projects with anonymous codes, scorecards |
| `PARTICIPANT` | Only `/api/v1/participant/*`: the leader's own team |

Each operation in `openapi.json` carries an `x-roles` list and a **Roles** line in its description. A signed-in user
without one of those roles gets `403 admin access required`. Hide what a role can't use in your UI, but rely on the
API to enforce it (the ORG. admin panel's `can` helpers in `src/lib/queries.ts` mirror these sets).

### Getting an account

- **Team leader:** created by registering a team. After payment (or straight away for a free or waived team) the
  leader gets an activation email; `GET /auth/activation?token=` previews it and `POST /auth/activate
  {token, password}` sets the password and returns tokens.
- **Staff and judges:** a `SUPER_ADMIN` invites them with `POST /auth/admin/invites {email, role}`; the invitee
  accepts with `POST /auth/admin/invites/accept {token, password}`.
- **First super admin:** `uv run python scripts/create_admin.py --email you@example.com` on the server.
- **Forgot password:** `POST /auth/password/forgot {email}` always answers `202` (it never reveals whether the
  address exists); the email links to your reset page, which calls `POST /auth/password/reset {token, password}`.
  The link base comes from `PARTICIPANT_APP_URL` / `ADMIN_APP_URL` on the server.

## 5. Errors

Every non-2xx response is JSON in one of three shapes:

```jsonc
// Most errors (HTTPException): a short reason you can show or branch on
{ "detail": "team name is already registered" }

// Validation (422): FastAPI's list, one entry per bad field
{ "detail": [ { "loc": ["body", "members"], "msg": "Value error, ACM ID is required when ACM membership is selected", "type": "value_error" } ] }

// Infrastructure (ApiError): a stable code plus the request id
{ "error": { "code": "database_unavailable", "request_id": "1e07dbb1-…" } }
```

A helper that turns any of them into one message (this is what both apps do):

```ts
export function apiErrorMessage(data: unknown, fallback = "Something went wrong.") {
  if (data && typeof data === "object") {
    if ("detail" in data) {
      const detail = (data as { detail: unknown }).detail
      if (typeof detail === "string") return detail
      if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg).replace(/^Value error, /, "")
    }
    if ("error" in data) return String((data as { error: { code?: string } }).error?.code ?? fallback)
  }
  return fallback
}
```

| Status | Meaning | Typical `detail` |
|---|---|---|
| 400 | Bad or expired token in the body | `invalid activation token`, `invalid payment signature` |
| 401 | No, expired or bad credentials | `invalid access token` (refresh and retry), `invalid credentials`, `invalid refresh token` (sign the user out) |
| 403 | Signed in but not allowed, or a window is closed | `admin access required`, `registration is closed`, `submission is closed` |
| 404 | The id doesn't exist (or isn't yours) | `team not found`, `registration not found` |
| 409 | Conflicts with current state | `team name is already registered`, `submission was changed in another window; reload to continue` |
| 410 | A link was already used or expired | `this invite link has already been used` |
| 422 | Validation failed | list form above, or a business rule such as `choose one of this event's problem statements` |
| 429 | Rate limited | `too many login attempts` (see [Rate limits](#8-rate-limits)) |
| 502 | An upstream (Razorpay, Google, S3) failed | `payment provider is unavailable` |
| 503 | A feature isn't configured on this server, or the database is down | `payment provider is not configured`, `photo storage is not configured`, `database_unavailable` |

Every response carries `X-Request-ID`. Send your own (any string) to correlate logs; otherwise the API makes one.
Quote it when you ask for help.

## 6. Conventions

- **Base path:** `/api/v1`. Health is `/healthz`.
- **JSON** request and response bodies, `content-type: application/json`. CSV downloads (`text/csv`) and the
  protected resource viewer (`application/pdf`) are the exceptions, plus the live stream (`text/event-stream`).
- **Ids** are UUID strings. Submission codes, credential codes (`HMX-XXXX-XXXX`) and invite tokens are strings.
- **Times** are ISO 8601 with an offset, stored in UTC (`2026-11-11T19:33:45.871+00:00`). Send times with an
  offset; the event's own timezone is `event.timezone` in `/public/event`.
- **Money** is in minor units (paise): `20000` is ₹200. Fields end in `_minor` or are named `amount`.
- **Enums** are UPPER_CASE strings (`PAYMENT_PENDING`, `PAID`, `ACCEPTED`…). The spec lists the allowed values for
  inputs.
- **Pagination** on admin lists: `page` (from 1) and `page_size` (max 100); responses carry `items`, `page`,
  `page_size` and `total`. The audit
  log uses `limit` and `before` (a timestamp cursor).
- **Optimistic concurrency** on submissions: send back the `version` you loaded; a stale one returns `409`.
- **Words:** an "event" in the UI is a **stage** in the API (`stage_id`), and a "problem statement" is a **track**
  (`track_id`). One edition (HackMatrix 2026) has several stages, e.g. the online round and the finals.

## 7. Flows

### Public site

| Need | Call |
|---|---|
| Schedule, windows, prices, registration open/closed | `GET /api/v1/public/event` |
| Banner notices | `GET /api/v1/public/site-notices` |
| Results once released | `GET /api/v1/public/leaderboard` (`released: false` until staff release a round) |
| Photo gallery | `GET /api/v1/public/gallery` |
| Instagram posts | `GET /api/v1/public/instagram` (`configured: false` without a token) |
| Verify a certificate or badge | `GET /api/v1/public/credentials/{code}` |
| Contact form | `POST /api/v1/public/contact {name, email, subject, message, website}` (`website` is a honeypot: leave it empty) |

These are cache-friendly; revalidate every 30 to 60 seconds rather than on every page view.

### Team registration and payment

```
POST /api/v1/registrations ──▶ 202 checkout {order_id, amount, key_id, …}   (paid event)
                           └─▶ 202 {status: "PAID", fee_waived: true}       (free event or waived invite)
Razorpay Checkout (browser) ──▶ handler gets {razorpay_order_id, razorpay_payment_id, razorpay_signature}
POST /api/v1/payments/razorpay/confirm ──▶ {status: "paid"}  → leader gets the activation email
GET  /api/v1/registrations/payment-status?order_id=…  ──▶ poll if the browser lost the callback
```

1. Send the team. The server decides the price from the members' ACM status and the event's discounts; never send an
   amount.

   ```json
   {
     "team_name": "Null Pointers",
     "rules_consent": true,
     "privacy_consent": true,
     "members": [
       {"role": "leader", "full_name": "A Leader", "email": "lead@example.com", "phone": "9812345678",
        "college": "Adamas University", "is_acm_member": true, "acm_id": "1234567"},
       {"role": "member", "full_name": "B Member", "email": "b@example.com", "phone": "9812345679",
        "college": "Other College", "is_acm_member": false}
     ],
     "invite_token": ""
   }
   ```

   Two to four members, exactly one leader. `invite_token` comes from an organiser's team invite link
   (`GET /api/v1/registrations/invite?token=` previews it).

2. Open Razorpay Checkout with what came back:

   ```ts
   const checkout = await (await fetch("/api/registrations", { method: "POST", body: JSON.stringify(team) })).json()
   new Razorpay({
     key: checkout.key_id,
     order_id: checkout.order_id,
     amount: checkout.amount,
     currency: checkout.currency,
     name: checkout.team_name,
     handler: (result) => fetch("/api/payments/confirm", { method: "POST", body: JSON.stringify(result) }),
   }).open()
   ```

3. Confirm. The server verifies the signature and that Razorpay captured the payment, so a forged callback cannot
   mark a team paid. The Razorpay webhook (`/payments/razorpay/webhook`) does the same independently, so a team is
   marked paid even if the browser closes.

4. The checkout page never receives the activation token: the leader activates from the email.

Sending the same team again while its order is unpaid returns the same checkout. A different team from the same
leader while a payment is pending gets `409 registration already pending; finish that payment or try again in 30
minutes`. Unpaid registrations get a reminder email after an hour and lapse an hour after that.

### Participant dashboard (team leader)

| Need | Call |
|---|---|
| Everything for the home screen: team, members, status, finals, current submission | `GET /participant/dashboard` |
| Edit the roster while edits are open | `POST /participant/team/members`, `PATCH`/`DELETE /participant/team/members/{id}`, `POST /participant/team/leader` |
| Events the team is in, their problem statements and the team's submission for each | `GET /participant/stages` |
| Start a timed event (problem statements unlock) | `POST /participant/stages/{stage_id}/start` |
| Pick a problem statement | `POST /participant/stages/{stage_id}/track {track_id}` |
| Save a draft | `PUT /participant/stages/{stage_id}/submission` (body below) |
| Final submit (locks it) | `POST /participant/stages/{stage_id}/submission/finalize` |
| Open a protected resource (PDF, watermark it yourself) | `GET /participant/stages/{s}/tracks/{t}/resources/{index}` |
| Announcements, results, certificates | `GET /participant/announcements`, `/results`, `/credentials` |
| Finals RSVP | `POST /participant/rsvp {answer: "CONFIRMED" \| "DECLINED"}` |

Submission draft body. Send back the `version` you loaded (0 for a new draft). Built-in fields are top-level strings;
the event's custom questions go in `answers`, keyed by field `key`. Each stage in `/participant/stages` lists its form
under `form.fields` (`key`, `label`, `type`, `required`, `builtin`):

```json
{
  "version": 0,
  "project_name": "Crop Doctor",
  "github_url": "https://github.com/team/crop-doctor",
  "pitch_deck_url": "https://drive.google.com/…",
  "demo_url": "", "video_url": "", "documentation_url": "", "description": "",
  "answers": {"sdg": "SDG 2", "tech_stack": "FastAPI, Next.js"}
}
```

#### Live notifications (Server-Sent Events)

`GET /api/v1/participant/notifications/stream` keeps one connection open per dashboard:

```
retry: 5000
event: ready
data: {}

event: notification
data: {"kind":"announcement","title":"…","body":"…","level":"INFO", …}

: ping
```

`ready` arrives on every (re)connect: refetch whatever you show, since you may have missed events while offline.
`notification` is one JSON object, always with `kind`, `title`, `body` and `level` (`INFO`, …); show it as a toast
and refetch what `kind` points at. `: ping` lines every 20 seconds keep proxies
from closing an idle stream. `EventSource` can't send an `Authorization` header, so relay the stream through your
server (see `proxyParticipantStream` in `hackm/frontend/src/lib/participant-backend.ts`), and turn off response
buffering on any proxy in front of it.

### Judges

`GET /judge/assignments` lists the judge's projects (anonymous codes, no team names). `GET
/judge/assignments/{id}` shows one with the round's rubric. `PUT …/scorecard {scores, comments}` saves a draft (scores
keyed by metric id, e.g. `{"technical": 5}`) and `POST …/scorecard/submit` with the same body submits it. Scores outside a metric's range return `422`.

### Building an admin tool

Use the same BFF pattern with `portal: "admin"`. Start from `admin-hackm`, which is a complete client for every
`/api/v1/admin/*` route: its pages are a good index of which call does what. A few patterns to know:

- **Uploads** (gallery photos, certificate templates, email attachments) go straight from the browser to S3 or R2. Ask
  for a presigned target (`POST /admin/gallery/uploads`, `/admin/credentials/uploads`, `/admin/email-campaigns/uploads`),
  upload the file there, then pass the returned key or URL to the call that uses it (e.g. `POST /admin/gallery`).
- **Exports** are background jobs: `POST /admin/exports {dataset, fields, filters, scope}` returns `202`
  (`GET /admin/exports/datasets` lists datasets and fields), poll `GET /admin/exports/{id}`
  until it's ready, then download `GET /admin/exports/{id}/download` (CSV).
- **Bulk actions** have their own routes (`/admin/registrations/bulk`, `/admin/rounds/{id}/assignments/bulk`), so
  don't loop single calls.

## 8. Rate limits

Limits are counted in Postgres, so they hold across workers. Over the limit you get `429` with the reason in `detail`.

| Action | Limit |
|---|---|
| Login | 5 tries per email per client IP, 30 per email, per 15 minutes; then blocked for 15 minutes even with the right password |
| Password reset request | 3 per email per hour |
| Registration | 10 per leader email per hour |
| Contact form | 5 per email per hour |
| Finals RSVP link | 20 per link per hour |
| Owner flush codes | 3 per hour |

Forward the browser's IP as `X-Client-IP` from your BFF, or every user shares your server's login budget.

## 9. Support and troubleshooting

### Common problems

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser console: "blocked by CORS policy" | Calling the API from browser JavaScript | Call it from your server (section 3) |
| Every call returns `401 invalid access token` | Missing `Bearer ` prefix, token expired, or keys changed on the server | Refresh once on 401; if that also fails, sign the user in again |
| Users get signed out at random, often when a page loads several things at once | Parallel requests each refreshed with the same refresh token; the first rotated it and the rest were refused | Share one in-flight refresh per refresh token |
| Signed out exactly 15 minutes after login | The access cookie expired with the JWT, so there was nothing to refresh | Give the cookies a long `maxAge` and rely on 401 + refresh |
| Correct password returns `401 invalid credentials` | Wrong `portal` for the account (a leader on `admin`, or staff on `participant`), or the account isn't activated | Use the right portal; a pending leader should use the activation email or forgot-password |
| `403 admin access required` | The account lacks a role in the operation's `x-roles` | Ask a super admin to change the roles (`PATCH /admin/staff/{id} {roles: [...]}`) |
| `429 too many login attempts` for everyone | Your BFF isn't forwarding `X-Client-IP` | Forward the browser IP |
| `503 payment provider is not configured` | `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` missing on the server | Set them (test keys locally) |
| Paid, but the team still shows unpaid | The browser closed before confirm and the webhook isn't reaching the server | Check the Razorpay webhook URL and `RAZORPAY_WEBHOOK_SECRET`; poll `/registrations/payment-status` |
| No emails locally | Without `RESEND_API_KEY` mail goes to the local sink (logged), not out | Set `RESEND_API_KEY`, or read the server log |
| Live notifications never arrive, or arrive in bursts | A proxy is buffering `text/event-stream` | Disable buffering for the stream path (`deploy/nginx/hackmatrix-api.conf` has the block) |
| Notifications reach only some dashboards | Several API workers without `REDIS_URL` | Set `REDIS_URL`, or run one worker |
| Protected resource returns `502` | The Google file isn't shared as "Anyone with the link can view" | Change the sharing setting |
| `503` on gallery or certificate uploads | No S3/R2 bucket configured | Set the `R2_*` or `S3_*` variables (repository README) |
| `409 submission was changed in another window` | The draft was saved elsewhere since you loaded it | Reload the submission and reapply the change |
| `/healthz` returns `503 database_unavailable` | Postgres is unreachable from the API | Check `DATABASE_URL` and the database container |

### Asking for help

- Check `/healthz` and the `detail` message first; most `4xx` reasons say what to do.
- For a bug or a missing field, open an issue on the `hackm-backend` repository with the method, path, status,
  response body and the `X-Request-ID` header. Never paste tokens or passwords.
- For anything about an account or a payment, email `support@hackmatrix.codes` with the team name and the request id.

## Keeping the docs current

```sh
uv run python scripts/export_openapi.py            # rebuild openapi.json and endpoints.md from the code
uv run python scripts/export_openapi.py --record   # rerun the test suite first to refresh response examples
```

- `src/openapi.py` builds the document. FastAPI supplies paths, parameters and request bodies; this module adds bearer
  auth, the `x-roles` of each route, the error schemas, tag descriptions, and the recorded responses.
- `src/openapi_examples.json` holds the recorded responses. `--record` runs `pytest` with `RECORD_OPENAPI_EXAMPLES=1`,
  which captures every test request and response, merges the response shapes into a schema, keeps the richest example,
  and scrubs tokens, passwords, signatures and signed URLs. It needs the test database (repository README).
- `tests/unit/test_openapi_docs.py` fails when the committed `openapi.json` or `endpoints.md` no longer matches the
  code. Run the first command and commit the result.
- After adding a route, write an integration test that calls it, then run `--record` so the route gets a response
  schema too.
- Give new request bodies a Pydantic model (not `dict`) so their schema shows up, and put a one-line docstring on the
  handler: it becomes the operation's description.
