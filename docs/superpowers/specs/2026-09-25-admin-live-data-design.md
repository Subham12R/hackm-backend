# Admin live-data integration design

**Status:** Approved 2026-09-25

**Owners:** `hackm-backend` owns the data and authorization contract; `admin-hackm` owns BFF transport and rendering.

**Scope:** Phase 3 operational data only.

## Goal

Replace every current fixture-backed admin operational surface with PostgreSQL-backed data while preserving the existing admin routes, controls, layout, and visual styling. A failed request must be visible as a loading, empty, or error state; it must never display fixture data as if it were real.

## In scope

- Dashboard overview: operator name, four metrics, registration series/split, latest ten registrations, and latest ten audit logs.
- Registrations: server-side date/category filtering, pagination, filter-consistent chart aggregates, payment state from payment records, and existing column controls.
- Teams: stored members, server-side filtering/pagination, persisted details review, and existing expandable rows.
- Submissions: persisted submission data, live evaluator directory, persisted assignment, and truthful disabled/error states when a submission is not assignable.
- Evaluations and audit logs: backend records instead of page constants, with existing filtering/sorting retained in the client where the current UI exposes it.
- A same-origin Next BFF that retries a request once after a valid refresh-token rotation, safely relays upstream errors, and never exposes backend tokens to browser JavaScript.
- Backend integration tests, migration coverage, admin lint/type/build checks, and a narrow browser verification against a seeded backend.

## Deferred

Challenge, contacts, announcements, analytics, event control, leaderboard, and settings remain explicit placeholder routes. Their APIs and UI state belong to Phase 4/5; this release must not invent records or substitute local data for them. Judge-specific anonymous projections are a separate judge UI/API slice; `JUDGE` must not retain access to broad admin projections.

## Non-negotiable constraints

- Do not alter admin page layout, navigation, visual hierarchy, or interaction placement.
- Do not add a fixture fallback, browser storage persistence, or a new frontend dependency.
- Amounts cross service boundaries as integer paise (`*_minor`), never floating point.
- The FastAPI service is authoritative for status, counts, money, review state, assignment, and audit records.
- Browser requests go only to `admin-hackm` `/api/*` routes. Those routes read host-only cookies and contact `BACKEND_URL` server-side.
- Backend mutations validate input, enforce role capability, write their durable state, then append an audit log in the same transaction.
- A BFF retries at most once after refresh. It does not retry non-401 responses, network failures, or mutations after an ambiguous upstream failure.
- Preserve the existing unrelated `hackm-backend/docker-compose.yml` worktree change.

## Data definitions

### Overview

`GET /api/v1/admin/overview` is the sole source for the dashboard. It returns:

```json
{
  "viewer_name": "Admin",
  "metrics": {
    "registrations": 12,
    "monetary_amount_minor": 240000,
    "submissions": 8,
    "total_users": 23
  },
  "registration_series": [{ "date": "2026-09-25", "registrations": 3 }],
  "registration_split": [{ "category": "Adamas ACM", "registrations": 2 }],
  "recent_registrations": [],
  "recent_logs": []
}
```

Metric rules:

- `registrations`: all persisted `Registration` rows.
- `monetary_amount_minor`: sum of `PaymentOrder.amount_minor` only where `status == "CAPTURED"`.
- `submissions`: paid teams represented by the submissions queue. This matches the existing submissions page, which includes a truthful `Draft` row when a paid team has not yet saved a project.
- `total_users`: all persisted `User` rows, regardless of lifecycle state.
- Time series and category split use all persisted registrations. Dates are backend timestamps rendered in the current UI’s `en-IN` format.
- `recent_registrations` and `recent_logs` contain at most ten newest rows using the same serializers as their dedicated endpoints.

### Registration list

`GET /api/v1/admin/registrations?date=YYYY-MM-DD&category=<category>&page=1&page_size=10`

```json
{
  "items": [
    {
      "registration_id": "uuid",
      "registered_at": "2026-09-25",
      "team_name": "Team",
      "leader": "Leader",
      "email": "leader@example.com",
      "phone": "9812345678",
      "college": "Adamas University",
      "members": 4,
      "acm": "Yes",
      "acm_members": 2,
      "category": "Adamas ACM",
      "registration_status": "PAID",
      "payment_status": "CAPTURED",
      "amount_minor": 20000
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 10,
  "daily_counts": [{ "date": "2026-09-25", "adamas": 1, "other": 0 }]
}
```

`category` is one of `Adamas ACM`, `Adamas only`, `Outer college`, or `Outer + ACM`. It is derived from stored members, never from a client-supplied classification. `payment_status` is derived from `PaymentOrder`: `NOT_STARTED` when no order exists, otherwise the persisted payment-order status. The UI may format those values for display but may not infer them from registration status.

### Team list and review

`GET /api/v1/admin/teams` accepts the same `date`, `category`, `page`, and `page_size` contract and returns `{ items, total, page, page_size, daily_counts }`. A team item contains `registration_id`, anonymous `team_id`, registration fields, stored members, derived category, and:

```json
"review": {
  "status": "PENDING | VERIFIED | DECLINED",
  "reviewed_at": "2026-09-25T10:00:00+00:00 | null",
  "reviewed_by": "Admin | null"
}
```

`POST /api/v1/admin/teams/{registration_id}/review` accepts `{ "status": "VERIFIED" | "DECLINED" }` and returns the updated `review`. The database stores the latest status, reviewer user ID, and review time on the registration. A default `PENDING` state has no reviewer or time. A state change creates one append-only audit event. Repeating the same state is idempotent and does not create a duplicate audit row.

### Evaluator directory and assignments

`GET /api/v1/admin/evaluators` returns active users with the `JUDGE` role only:

```json
{ "evaluators": [{ "email": "judge@example.com", "display_name": "Judge" }] }
```

`POST /api/v1/admin/submissions/{registration_id}/assign` retains the existing route and accepts `{ "assignee_email": "judge@example.com" }`. It requires a saved, `SUBMITTED` submission and an active `JUDGE` assignee. Its response carries the assigned evaluator object or `null`, not a fixture name. The list response includes `can_assign` so the unchanged control can be disabled when assignment is not permitted.

### Existing read projections

- `GET /api/v1/admin/submissions` returns persisted links, ISO timestamps, status, `registration_id`, `team_id`, `assigned_to`, and `can_assign`.
- `GET /api/v1/admin/evaluations` returns persisted score fields and ISO evaluation time. The page obtains its round/evaluator options from returned data; it does not carry fixture constants.
- `GET /api/v1/admin/logs?limit=100` returns append-only audit log projections. Overview consumes the newest ten through its own server-side query.

## Authorization

All generic admin reads require an admin role but exclude `JUDGE`; judges must not receive team identity, contact data, or broad admin lists through `/api/v1/admin/*`.

| Operation | Allowed roles |
|---|---|
| Operational reads and overview | `SUPER_ADMIN`, `EVENT_ADMIN`, `REGISTRATION_ADMIN`, `EVALUATION_ADMIN`, `SUPPORT_ADMIN`, `VIEWER` |
| Team review | `SUPER_ADMIN`, `EVENT_ADMIN`, `REGISTRATION_ADMIN` |
| Evaluator directory and submission assignment | `SUPER_ADMIN`, `EVENT_ADMIN`, `EVALUATION_ADMIN` |

The existing evaluation-write route is not redesigned in this scope. A future judge slice must expose a role-scoped anonymous projection rather than reuse a generic admin route.

## BFF behavior

Every BFF route that reads an authenticated admin session, including `/api/auth/me`, uses one shared server-only transport helper.

1. Read `hackmatrix_admin_access`; return a normalized 401 response if absent.
2. Forward the request to `BACKEND_URL` with the bearer token and `cache: "no-store"`.
3. If the upstream response is not 401, relay its JSON and status unchanged.
4. On the first 401 only, submit `hackmatrix_admin_refresh` to `/api/v1/auth/refresh`.
5. If refresh succeeds, set rotated host-only cookies with the current cookie attributes, retry the original request once with the new access token, and relay that result.
6. If refresh fails, clear both cookies and return a normalized 401. Client pages redirect to `/login` on this response.
7. If the backend is not configured, unreachable, or returns non-JSON, return a safe 503/502 JSON error. Do not leak URL, token, stack trace, or raw HTML.

The helper forwards query parameters unchanged, gives each request a bounded abort signal, and never retries a non-401 mutation.

## Client rendering and state rules

- Every data page begins in a data-shaped skeleton state. Skeletons occupy the current card/table/chart region; no layout is added or rearranged.
- Every page has exactly three terminal data states: populated, empty, or error. Errors use the existing text hierarchy and offer no fixture fallback.
- A 401 redirects to `/login`; an abort after unmount does not set state; a non-401 error remains visible inside the affected existing section.
- Registrations and teams refetch when date, category, or page changes. They reset to page 1 when a filter changes. Charts use the endpoint’s `daily_counts`, so charts always describe the active filter across all matching rows rather than the current page.
- Team review and evaluator assignment update local row state only after the backend confirms the mutation. The existing control is disabled while its request is pending.
- Status, money, date, and link display are formatting concerns in the client. Business classification, permissions, and lifecycle decisions remain on the backend.

## Verification

Backend tests use PostgreSQL migrations and real authenticated FastAPI requests. They must prove pagination/filter accuracy, aggregate semantics, authorization denials, evaluator eligibility, idempotent review writes, audit rows, assignment rules, and no judge access to identity-bearing admin projections.

The frontend currently has no test runner. Do not add one only for this slice. Verify the typed BFF and rendering integration with `npm run lint`, `npx tsc --noEmit`, `npm run build`, and a local browser pass against seeded backend data. Run targeted backend `pytest` before the suite.

## Acceptance checks

1. Searching the six in-scope admin pages finds no fixture arrays used for displayed operational data.
2. Reloading every in-scope page with a valid admin session displays PostgreSQL data only.
3. A backend outage, bad payload, or unauthorized request displays a real degraded state and never mocks a successful result.
4. The dashboard card totals and recent rows agree with direct authenticated backend responses.
5. Registration/team pagination and chart totals stay correct when filters change.
6. Team member rows, review state, assignment labels, evaluations, and logs survive reload because they are backend records.
7. A non-judge cannot be assigned; a judge cannot read generic admin PII; unauthorized mutations receive 403.
8. Existing page route names and visual layout remain unchanged.
