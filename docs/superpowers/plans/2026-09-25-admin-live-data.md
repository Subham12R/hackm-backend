# Admin Live-Data Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all fixture-backed Phase 3 admin operational data with authenticated PostgreSQL projections without changing the existing admin layout or navigation.

**Architecture:** FastAPI gains an explicit operational read model, server-owned pagination/filter aggregates, a persisted team-review state, and capability-scoped mutations. `admin-hackm` keeps browser traffic same-origin through small BFF routes backed by one refresh-safe server transport; typed client contracts adapt those projections into the existing page components and their loading/error/empty regions.

**Tech Stack:** FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, pytest, Next.js 16 App Router route handlers, React 19, TypeScript, Zod, ESLint.

**Spec:** `docs/superpowers/specs/2026-09-25-admin-live-data-design.md`

## Global Constraints

- Preserve the existing admin routes, controls, layout, and visual styling; change only data/state behavior and copy required to describe real states.
- Do not add a frontend dependency or a fixture/browser-storage fallback.
- Use integer paise in service contracts; format currency only in the client.
- Browser code calls only same-origin `/api/*`; `BACKEND_URL` and bearer tokens remain server-only.
- Every team-review/assignment mutation validates input, checks capability, persists first, and appends its audit record in the same transaction.
- A BFF refreshes once after upstream 401, never retries other failures or ambiguous mutations, and returns safe normalized JSON for unavailable upstream services.
- Preserve the unrelated `hackm-backend/docker-compose.yml` modification.

## File structure

### Backend

- Create `migrations/versions/0006_admin_live_data.py`: durable team-review columns and reversible migration.
- Modify `src/db/models/registration.py`: typed review fields on `Registration`.
- Modify `src/api/admin.py`: role helpers, serializers, paginated registration/team reads, overview, evaluator list, review mutation, constrained assignment, and bounded logs.
- Create `tests/integration/test_admin_operations.py`: PostgreSQL-backed tests for the new/changed operational contract and authorization behavior.
- Modify `tests/integration/test_admin_registrations.py`: retain the existing registration authorization assertion while adapting to the pagination envelope.

### Admin BFF and typed client boundary

- Create `src/lib/admin-backend.ts`: server-only authenticated backend transport, bounded refresh/retry, cookie rotation/clearing, and safe response conversion.
- Create `src/lib/admin-client.ts`: Zod response contracts, frontend view-model adapters, one JSON fetch helper, and shared ISO/currency/status formatting helpers.
- Modify `src/app/api/admin/{registrations,teams,submissions,evaluations,logs}/route.ts`: delegate to the shared transport and preserve query strings.
- Modify `src/app/api/admin/submissions/[registrationId]/assign/route.ts`: delegate the assignment mutation; retry only an explicit upstream 401 after a successful refresh, never an ambiguous failure.
- Modify `src/app/api/auth/me/route.ts`: delegate authenticated identity reads through the same transport so the sidebar participates in refresh rotation.
- Create `src/app/api/admin/overview/route.ts`, `src/app/api/admin/evaluators/route.ts`, and `src/app/api/admin/teams/[registrationId]/review/route.ts`: thin BFF delegates only.

### Admin pages

- Modify `src/app/(admin)/dashboard/page.tsx`: replace dashboard constants with overview data and data-shaped states.
- Modify `src/app/(admin)/dashboard/registrations/page.tsx`: consume paginated filtered registration data and server daily counts.
- Modify `src/app/(admin)/dashboard/teams/page.tsx`: remove generated members/fixtures and persist review changes.
- Modify `src/app/(admin)/dashboard/submissions/page.tsx`: load submissions/evaluators and persist assignment.
- Modify `src/app/(admin)/dashboard/evaluations/page.tsx`: load evaluations and derive filter values from live rows.
- Modify `src/app/(admin)/dashboard/logs/page.tsx`: load and format persisted audit logs.
- Modify `src/app/(admin)/dashboard/loading.tsx` only if its skeleton no longer represents the overview’s existing grid/sections.

## Task 1: Persist team-review state

**Files:**
- Create: `migrations/versions/0006_admin_live_data.py`
- Modify: `src/db/models/registration.py`
- Test: `tests/integration/test_admin_operations.py`

**Interfaces:**
- Produces `Registration.details_review_status: str`, `details_reviewed_at: datetime | None`, and `details_reviewed_by_user_id: UUID | None`.
- Produces a non-null database default `PENDING` for existing and future registrations.

- [ ] **Step 1: Write the migration-backed failing test**

```python
def test_team_review_defaults_to_pending_after_migration() -> None:
    registration = create_paid_registration()

    team = admin_get(f"/api/v1/admin/teams?page=1&page_size=10").json()["items"][0]

    assert team["review"] == {
        "status": "PENDING",
        "reviewed_at": None,
        "reviewed_by": None,
    }
```

- [ ] **Step 2: Run the test to verify the missing contract fails**

Run: `uv run pytest tests/integration/test_admin_operations.py::test_team_review_defaults_to_pending_after_migration -v`

Expected: FAIL because the team projection has no `review` field.

- [ ] **Step 3: Add the reversible migration and typed model fields**

```python
# upgrade
op.add_column("registrations", sa.Column(
    "details_review_status", sa.String(length=20), nullable=False,
    server_default="PENDING",
))
op.add_column("registrations", sa.Column("details_reviewed_at", sa.DateTime(timezone=True)))
op.add_column("registrations", sa.Column(
    "details_reviewed_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"),
))

# downgrade: drop the three columns in reverse dependency order
```

Model fields must mirror the migration. Remove the server default from the model only if SQLAlchemy should always supply the Python default; retain the migration default for old rows.

- [ ] **Step 4: Apply the migration and rerun the focused test**

Run: `uv run alembic upgrade head && uv run pytest tests/integration/test_admin_operations.py::test_team_review_defaults_to_pending_after_migration -v`

Expected: PASS; the read model emits `PENDING` with null metadata.

- [ ] **Step 5: Commit the schema boundary**

```bash
git add migrations/versions/0006_admin_live_data.py src/db/models/registration.py tests/integration/test_admin_operations.py
git commit -m "feat: persist admin team review state"
```

## Task 2: Build role-scoped admin serializers and paginated reads

**Files:**
- Modify: `src/api/admin.py`
- Modify: `tests/integration/test_admin_registrations.py`
- Modify: `tests/integration/test_admin_operations.py`

**Interfaces:**
- Consumes `date: date | None`, `category: RegistrationCategory | None`, `page: int`, and `page_size: int`.
- Produces `{"items": list[RegistrationRow | TeamRow], "total": int, "page": int, "page_size": int, "daily_counts": list[dict]}`.
- Produces `require_admin_roles(user, allowed_roles)` and excludes `JUDGE` from generic admin reads.

- [ ] **Step 1: Write focused failing authorization and pagination tests**

```python
def test_registration_filter_pagination_and_daily_counts_are_server_owned() -> None:
    create_registration("Adamas", college="Adamas University", acm=True, created_at=day_one)
    create_registration("Outer", college="Other College", acm=False, created_at=day_two)

    response = admin_get(
        "/api/v1/admin/registrations?category=Adamas%20ACM&page=1&page_size=1"
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["team_name"] == "Adamas"
    assert response.json()["daily_counts"] == [
        {"date": day_one.date().isoformat(), "adamas": 1, "other": 0}
    ]


def test_judge_cannot_read_identity_bearing_admin_tables() -> None:
    response = judge_get("/api/v1/admin/teams?page=1&page_size=10")
    assert response.status_code == 403
```

- [ ] **Step 2: Run the focused tests to verify current behavior fails**

Run: `uv run pytest tests/integration/test_admin_operations.py -k 'filter_pagination or judge_cannot' -v`

Expected: FAIL because list responses are unpaginated and `JUDGE` is accepted by the current broad role set.

- [ ] **Step 3: Implement role helpers and shared serializers before route bodies**

```python
ADMIN_READ_ROLES = {
    RoleCode.SUPER_ADMIN, RoleCode.EVENT_ADMIN, RoleCode.REGISTRATION_ADMIN,
    RoleCode.EVALUATION_ADMIN, RoleCode.SUPPORT_ADMIN, RoleCode.VIEWER,
}
TEAM_REVIEW_ROLES = {
    RoleCode.SUPER_ADMIN, RoleCode.EVENT_ADMIN, RoleCode.REGISTRATION_ADMIN,
}
ASSIGNMENT_ROLES = {
    RoleCode.SUPER_ADMIN, RoleCode.EVENT_ADMIN, RoleCode.EVALUATION_ADMIN,
}


def require_admin_roles(user: User, allowed: set[RoleCode]) -> None:
    if not ({role.code for role in user.roles} & allowed):
        raise HTTPException(403, "admin access required")
```

Create `_registration_row`, `_team_row`, `_category_for_members`, `_payment_status_for_registration`, `_daily_counts`, and `_paginate` helpers. Fetch stored `RegistrationMember` rows; never derive names, phones, email addresses, or ACM IDs from a team name.

Use the current backend’s bounded in-memory derived-category filter and add this exact code comment beside it:

```python
# ponytail: categories depend on member aggregates; use a grouped SQL projection if event volume makes this O(n) request path measurable.
```

- [ ] **Step 4: Implement query validation and envelopes**

Use FastAPI `Query(ge=1, le=100)` for pagination. Parse `date` as an ISO `date`; use an explicit category enum/value set so invalid filters receive 422. Compute `daily_counts` over every filtered row before slicing the page.

Change both GET routes to return the envelope and update the registration test’s row lookup to inspect `response.json()["items"]`.

- [ ] **Step 5: Run focused integration tests**

Run: `uv run pytest tests/integration/test_admin_registrations.py tests/integration/test_admin_operations.py -v`

Expected: PASS; pagination, categories, real members, payment-derived status, and judge denial are proven against PostgreSQL.

- [ ] **Step 6: Commit the read-model boundary**

```bash
git add src/api/admin.py tests/integration/test_admin_registrations.py tests/integration/test_admin_operations.py
git commit -m "feat: paginate admin operational read models"
```

## Task 3: Add overview, review, evaluator, assignment, and log contracts

**Files:**
- Modify: `src/api/admin.py`
- Modify: `tests/integration/test_admin_operations.py`

**Interfaces:**
- Produces `GET /api/v1/admin/overview`.
- Produces `POST /api/v1/admin/teams/{registration_id}/review` with `TeamReviewRequest`.
- Produces `GET /api/v1/admin/evaluators` and normalized assignment responses.
- Consumes `AssignRequest.assignee_email` and returns `{"assigned_to": {"email": str, "display_name": str} | None}`.

- [ ] **Step 1: Write failing operation and permission tests**

```python
def test_overview_uses_captured_money_and_shared_recent_rows() -> None:
    paid = create_registration("Paid", payment_status="CAPTURED", amount_minor=20000)
    create_registration("Pending", payment_status="CREATED", amount_minor=90000)

    overview = admin_get("/api/v1/admin/overview").json()

    assert overview["metrics"]["registrations"] == 2
    assert overview["metrics"]["monetary_amount_minor"] == 20000
    assert overview["recent_registrations"][0]["registration_id"] == str(paid.id)


def test_registration_admin_can_review_once_and_audit_the_change() -> None:
    response = registration_admin_post(
        f"/api/v1/admin/teams/{registration_id}/review", json={"status": "VERIFIED"}
    )
    repeated = registration_admin_post(
        f"/api/v1/admin/teams/{registration_id}/review", json={"status": "VERIFIED"}
    )

    assert response.json()["review"]["status"] == "VERIFIED"
    assert repeated.status_code == 200
    assert count_audit("Team details reviewed", str(registration_id)) == 1


def test_assignment_requires_active_judge_and_evaluation_capability() -> None:
    assert evaluation_admin_get("/api/v1/admin/evaluators").json()["evaluators"] == [
        {"email": judge_email, "display_name": "Judge"}
    ]
    assert evaluation_admin_post(assign_path, json={"assignee_email": non_judge_email}).status_code == 404
    assert registration_admin_post(assign_path, json={"assignee_email": judge_email}).status_code == 403
```

- [ ] **Step 2: Run the new operation tests and observe the expected failures**

Run: `uv run pytest tests/integration/test_admin_operations.py -k 'overview or review or assignment' -v`

Expected: FAIL because overview/evaluator/review routes do not exist, assignment accepts non-judges, and no idempotent review audit exists.

- [ ] **Step 3: Implement the overview projection**

Use `func.count` for registrations and users, and sum only `PaymentOrder` rows with `status == "CAPTURED"`. Reuse the registration/log serializers for recent rows and use the authenticated admin’s `display_name` for `viewer_name`. Return integer `monetary_amount_minor`.

- [ ] **Step 4: Implement review and evaluator mutations with capability checks**

Define:

```python
class TeamReviewRequest(BaseModel):
    status: Literal["VERIFIED", "DECLINED"]
```

For a changed review state, set the status/time/reviewer, add `AuditLog(action="Team details reviewed", target_type="registration", target_id=str(registration.id), metadata_json={"status": payload.status})`, then commit once. Return the serializer’s review object. For the same status, return the current review without an audit insert.

Query evaluators through `User.roles`, requiring `UserStatus.ACTIVE` and `RoleCode.JUDGE`. Assignment must require an existing `SUBMITTED` `Submission`; reject non-judge/inactive recipients with the same 404 response to avoid user-directory probing. Add `can_assign` to each submission row and do not expose assignment controls as successful when no saved submission exists.

- [ ] **Step 5: Bound audit-log retrieval**

Give `GET /logs` a validated `limit: int = Query(100, ge=1, le=100)`. Reuse one `_audit_log_row` serializer for the dedicated route and overview. Keep audit records read-only.

- [ ] **Step 6: Run the operation suite**

Run: `uv run pytest tests/integration/test_admin_operations.py -v`

Expected: PASS, including captured-only money, role denials, active-judge filtering, idempotent review, assignment audit, and bounded logs.

- [ ] **Step 7: Commit operational APIs**

```bash
git add src/api/admin.py tests/integration/test_admin_operations.py
git commit -m "feat: add live admin overview and operations"
```

## Task 4: Build the shared Next admin backend transport

**Files:**
- Create: `/Users/subham/Dev/admin-hackm/src/lib/admin-backend.ts`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/api/admin/registrations/route.ts`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/api/admin/teams/route.ts`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/api/admin/submissions/route.ts`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/api/admin/evaluations/route.ts`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/api/admin/logs/route.ts`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/api/admin/submissions/[registrationId]/assign/route.ts`

**Interfaces:**
- Produces `proxyAdmin(request: Request, upstreamPath: string, options?: { retryOn401?: boolean }): Promise<NextResponse>`.
- Consumes `hackmatrix_admin_access` and `hackmatrix_admin_refresh` host-only cookies.
- Produces normalized JSON 401/502/503 responses and propagates upstream status/body when valid JSON is returned.

- [ ] **Step 1: Add a manual failure matrix before implementation**

Document and execute these cases with the local BFF after each implementation step: no access cookie → 401; expired access plus valid refresh → one successful retry with rotated cookies; expired access plus invalid refresh → cleared cookies and 401; unavailable `BACKEND_URL` → 503; malformed upstream body → 502; upstream 422/403 → same status/body; assignment upstream 401 after refresh remains one retried request only.

- [ ] **Step 2: Implement the server-only transport helper**

```ts
export async function proxyAdmin(
  request: Request,
  upstreamPath: string,
  options: { retryOn401?: boolean } = {}
): Promise<NextResponse>
```

Use `cookies()` only inside the helper. Perform one `fetch` with `cache: "no-store"` and an `AbortSignal.timeout(10_000)`. On the first 401 with a refresh cookie, call backend `/api/v1/auth/refresh`, set rotated cookies with the exact current `httpOnly`, `sameSite`, `secure`, `path`, and max-age values, then repeat the original upstream request once. Never retry an unreachable request or a response other than first-attempt 401. Parse upstream JSON defensively from text; do not leak raw non-JSON errors.

- [ ] **Step 3: Reduce existing BFF routes to delegates**

Each admin GET route calls `proxyAdmin(request, "/api/v1/admin/<resource>")`, forwarding `request.nextUrl.search`; `/api/auth/me` calls the same helper with `/api/v1/auth/me`. The assignment route passes the original JSON body once. Remove duplicated cookie/backend/fetch code.

- [ ] **Step 4: Run static verification before page work**

Run: `cd /Users/subham/Dev/admin-hackm && npm run lint && npx tsc --noEmit`

Expected: PASS; route handler signatures compile under Next.js 16.

- [ ] **Step 5: Commit the BFF consolidation**

```bash
cd /Users/subham/Dev/admin-hackm
git add src/lib/admin-backend.ts src/app/api/admin src/app/api/auth/me/route.ts
git commit -m "feat: centralize admin backend transport"
```

## Task 5: Expose new BFF routes and typed client contracts

**Files:**
- Create: `/Users/subham/Dev/admin-hackm/src/app/api/admin/overview/route.ts`
- Create: `/Users/subham/Dev/admin-hackm/src/app/api/admin/evaluators/route.ts`
- Create: `/Users/subham/Dev/admin-hackm/src/app/api/admin/teams/[registrationId]/review/route.ts`
- Create: `/Users/subham/Dev/admin-hackm/src/lib/admin-client.ts`

**Interfaces:**
- Produces `getAdminJson(url, schema, signal)` and Zod schemas for every Phase 3 BFF envelope.
- Produces view models with camelCase fields for the six existing page components.

- [ ] **Step 1: Define the client contract before replacing page data**

```ts
export async function getAdminJson<T>(
  url: string,
  schema: z.ZodType<T>,
  signal?: AbortSignal
): Promise<T> {
  const response = await fetch(url, { signal })
  if (response.status === 401) {
    window.location.assign("/login")
    throw new Error("sign-in-required")
  }
  if (!response.ok) throw new Error("admin-data-unavailable")
  return schema.parse(await response.json())
}
```

Define schemas for overview, paginated rows, review response, evaluators, submissions, evaluations, and logs. Map snake_case response fields once in this module; page components must not duplicate API field declarations.

- [ ] **Step 2: Add the thin new route handlers**

```ts
export function GET(request: Request) {
  return proxyAdmin(request, "/api/v1/admin/overview")
}
```

The evaluator GET is identical with its path. The review POST forwards its dynamic registration ID and original request JSON with `retryOn401: true`; the helper’s retry remains bounded to a verified 401-before-response case.

- [ ] **Step 3: Verify the contract boundary compiles**

Run: `cd /Users/subham/Dev/admin-hackm && npm run lint && npx tsc --noEmit`

Expected: PASS; no page imports a backend-only cookie module and no BFF route exposes credentials.

- [ ] **Step 4: Commit the contract layer**

```bash
cd /Users/subham/Dev/admin-hackm
git add src/lib/admin-client.ts src/app/api/admin/overview src/app/api/admin/evaluators src/app/api/admin/teams
git commit -m "feat: add typed admin data contracts"
```

## Task 6: Replace dashboard, registrations, and teams fixtures

**Files:**
- Modify: `/Users/subham/Dev/admin-hackm/src/app/(admin)/dashboard/page.tsx`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/(admin)/dashboard/registrations/page.tsx`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/(admin)/dashboard/teams/page.tsx`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/(admin)/dashboard/loading.tsx` only if needed

**Interfaces:**
- Consumes `overviewSchema`, `registrationsPageSchema`, `teamsPageSchema`, `getAdminJson`, and `POST /api/admin/teams/:id/review`.
- Produces no displayed operational fixture constants in these files.

- [ ] **Step 1: Remove fixtures and introduce initial loading state**

For each page, initialize `data` as `null`, `error` as `""`, and an `AbortController` inside `useEffect`. While `data === null` and no error exists, render skeleton cells/bars in the existing card/table/chart regions. Do not change grid classes, heading placement, navigation, or chart component choice.

- [ ] **Step 2: Wire dashboard overview**

Replace `metrics`, `registrationSeries`, `registrationSplit`, `recentRegistrations`, and `recentLogs` with `/api/admin/overview`. Display `monetaryAmountMinor / 100` with `Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 })`. Keep the existing card/series/table markup and cap recent rows through the backend response.

- [ ] **Step 3: Wire server-owned registration filters and pagination**

Build the request URL from `date`, `category`, `page`, and `PAGE_SIZE`; reset page to one whenever date/category changes. Use `total` for pagination text and `dailyCounts` directly for the chart. Map backend payment states to the existing badge palette without inventing failure values. On a 422/503/parse failure, show the error in the current section and clear displayed rows rather than retaining stale/mock data.

- [ ] **Step 4: Wire real teams and durable review**

Delete `registrations`, `memberNames`, `acmIdFor`, and `getTeamMembers`. Render the response’s stored `members`, including actual role, ACM status, and ACM ID. Use the response’s `review.status` for the existing select. On change, disable that select, POST the review, replace only the confirmed team review in state, and show an inline existing-section error if it fails. Keep expanded-row placement unchanged.

- [ ] **Step 5: Run admin static checks**

Run: `cd /Users/subham/Dev/admin-hackm && npm run lint && npx tsc --noEmit && npm run build`

Expected: PASS; all three pages compile without fixture data or generated team members.

- [ ] **Step 6: Commit the first live page group**

```bash
cd /Users/subham/Dev/admin-hackm
git add 'src/app/(admin)/dashboard/page.tsx' 'src/app/(admin)/dashboard/registrations/page.tsx' 'src/app/(admin)/dashboard/teams/page.tsx' 'src/app/(admin)/dashboard/loading.tsx'
git commit -m "feat: connect dashboard registrations and teams"
```

## Task 7: Replace submissions, evaluations, and logs fixtures

**Files:**
- Modify: `/Users/subham/Dev/admin-hackm/src/app/(admin)/dashboard/submissions/page.tsx`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/(admin)/dashboard/evaluations/page.tsx`
- Modify: `/Users/subham/Dev/admin-hackm/src/app/(admin)/dashboard/logs/page.tsx`

**Interfaces:**
- Consumes typed submission/evaluator/evaluation/log contracts.
- Produces confirmed-only evaluator assignments and no fixture arrays/options.

- [ ] **Step 1: Wire submissions and evaluator directory**

Fetch submissions and evaluators in parallel with one `AbortController`. Replace static evaluator options with active judge response rows. Initialize assignment labels from `assignedTo`; submit `{ assignee_email }` to the current BFF assignment route; disable a row while saving or when `canAssign` is false. Use ISO dates from the response for the existing start/end cells and render `—` only for a null real timestamp.

- [ ] **Step 2: Wire live evaluations**

Delete fixture evaluations, rounds, and evaluators. Fetch evaluations once, derive distinct round/evaluator select options from returned data, and preserve the existing local search/sort UI. Map the backend’s `problem_solving`/`ui_ux` scores in `admin-client.ts`, not in JSX.

- [ ] **Step 3: Wire persisted logs**

Delete `auditLogs`, fetch `/api/admin/logs?limit=100`, and format ISO timestamps with the existing `en-IN` formatter. Preserve the read-only table and its established admin-row presentation, but calculate counts and keys from response data.

- [ ] **Step 4: Verify static correctness**

Run: `cd /Users/subham/Dev/admin-hackm && npm run lint && npx tsc --noEmit && npm run build`

Expected: PASS; all three pages compile without static operational fixtures.

- [ ] **Step 5: Commit the second live page group**

```bash
cd /Users/subham/Dev/admin-hackm
git add 'src/app/(admin)/dashboard/submissions/page.tsx' 'src/app/(admin)/dashboard/evaluations/page.tsx' 'src/app/(admin)/dashboard/logs/page.tsx'
git commit -m "feat: connect admin operational tables"
```

## Task 8: Verify the complete live-data flow

**Files:**
- Modify: `README.md` in `/Users/subham/Dev/admin-hackm` only if its fixture statement is now inaccurate.
- Modify: `README.md` in `/Users/subham/Dev/hackm-backend` only if its endpoint list needs the overview/evaluator/review contract listed.
- Modify: `docs/admin-live-data-delivery-sheet.md`: replace each `Planned` status with verified evidence, not claims.

**Interfaces:**
- Consumes both completed codebases and a disposable local admin/judge/registration/submission fixture set.
- Produces command output and browser evidence for every acceptance criterion.

- [ ] **Step 1: Run targeted backend tests**

Run: `cd /Users/subham/Dev/hackm-backend && uv run pytest tests/integration/test_admin_registrations.py tests/integration/test_admin_operations.py -v`

Expected: PASS; migration, aggregates, auth denial, reviews, evaluator eligibility, assignments, and logs are covered.

- [ ] **Step 2: Run the complete backend suite**

Run: `cd /Users/subham/Dev/hackm-backend && uv run pytest`

Expected: PASS; report any unavailable `TEST_DATABASE_URL` honestly rather than claiming success.

- [ ] **Step 3: Run complete admin checks**

Run: `cd /Users/subham/Dev/admin-hackm && npm run lint && npx tsc --noEmit && npm run build`

Expected: PASS with no type or route-handler errors.

- [ ] **Step 4: Perform browser verification against local seeded data**

Use `agent-browser` after both local services run. Check: valid login; overview values equal authenticated API response; registration/team date/category/page changes update count/chart/table consistently; real stored team members render; review survives reload; active judge assignment survives reload; evaluations/logs render; expired access refreshes; invalid refresh redirects to login; backend shutdown shows error/skeleton rather than fixture data.

- [ ] **Step 5: Reconcile docs and inspect both diffs**

Run:

```bash
cd /Users/subham/Dev/hackm-backend && git diff --check && git status --short
cd /Users/subham/Dev/admin-hackm && git diff --check && git status --short
```

Update READMEs and the delivery sheet only with actual verification results. Do not stage or alter the pre-existing backend `docker-compose.yml` modification.

- [ ] **Step 6: Commit documentation and verification evidence**

```bash
cd /Users/subham/Dev/hackm-backend
git add README.md docs/admin-live-data-delivery-sheet.md docs/superpowers/specs/2026-09-25-admin-live-data-design.md docs/superpowers/plans/2026-09-25-admin-live-data.md
git commit -m "docs: record admin live-data integration"
```

## Plan self-review

- **Spec coverage:** Tasks 1–3 implement all schema, authorization, endpoint, aggregate, mutation, and audit requirements. Tasks 4–5 implement BFF transport and typed contracts. Tasks 6–7 replace every in-scope fixture page without layout work. Task 8 covers verification and docs. Placeholder routes remain deferred by design.
- **Placeholder scan:** No unresolved behavior is deferred inside the Phase 3 scope. The only deliberate deferral is the named Phase 4/5 placeholder set.
- **Type consistency:** Backend list envelopes use `items`, `total`, `page`, `page_size`, and `daily_counts`; the client contract maps those once to page models. Assignment always accepts `assignee_email`; team review always accepts uppercase `VERIFIED`/`DECLINED` and returns a `review` object.
- **Risk check:** Server category filtering initially performs a bounded in-memory member-derived classification and records its upgrade path with a required `ponytail:` comment. It preserves correctness now without inventing a premature denormalized column.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-25-admin-live-data.md`.

1. **Subagent-driven (recommended):** execute one reviewed task at a time with fresh task context.
2. **Inline:** execute in this session with review checkpoints after each task.
