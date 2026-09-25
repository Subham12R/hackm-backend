# Admin live-data delivery sheet

**Spec:** `docs/superpowers/specs/2026-09-25-admin-live-data-design.md`

**Plan:** `docs/superpowers/plans/2026-09-25-admin-live-data.md`

**Scope:** Phase 3 operational data; preserve the existing admin UI/layout.

## Delivery tracker

| ID | Deliverable | Backend owner | Admin consumer | Evidence required | Status |
|---|---|---|---|---|---|
| D1 | Team review schema: status, reviewer, time | Migration `0006_admin_live_data.py` | Teams review select | Upgrade/downgrade and persisted review test | Verified |
| D2 | Capability helpers; no broad `JUDGE` access | `src/api/admin.py` | All BFF reads | Judge receives 403; privileged mutations succeed | Verified |
| D3 | Paginated registrations with payment-derived status and daily counts | `GET /admin/registrations` | Registrations page | Filter/page/aggregate integration tests | Verified |
| D4 | Paginated teams with stored members/review and daily counts | `GET /admin/teams` | Teams page | Member/review/filter integration tests | Verified |
| D5 | Dashboard overview aggregate projection | `GET /admin/overview` | Dashboard page | Captured-money/count/recent-row integration test | Verified |
| D6 | Active judge directory and constrained assignment | `GET /admin/evaluators`, assignment POST | Submissions page | Inactive/non-judge rejection and audit test | Verified |
| D7 | Live submissions, evaluations, logs projections | Existing GETs, normalized fields | Three admin pages | Real row and ISO-time integration tests | Verified |
| D8 | Shared refresh-safe BFF transport | `src/lib/admin-backend.ts` | All admin API routes | One confirmed-401 refresh/retry; safe 502/503 checks | Implemented; runtime refresh path unverified |
| D9 | Typed client contract and no-fixture data states | `src/lib/admin-client.ts` | Six in-scope pages | Lint, type check, build, browser evidence | Verified by static checks |
| D10 | Data-shaped loading/error/empty states | Existing page regions only | Six in-scope pages | Browser screenshots/checks with success, empty, and failure responses | Implemented; browser states unverified |
| D11 | End-to-end verification and doc reconciliation | Both repositories | Both repositories | Targeted pytest, pytest suite, lint, `tsc`, build, browser pass | Partial; browser flow unverified |

## Verification evidence

- `cd /Users/subham/Dev/hackm-backend && DATABASE_URL=... PYTHONPATH=. uv run pytest`: **39 passed**.
- `cd /Users/subham/Dev/admin-hackm && npm run lint`: no errors; three pre-existing warnings remain in `admin-sidebar.tsx` and `auth-09.tsx`.
- `npx tsc --noEmit` and `npm run build`: passed. The build retains the existing Geist Pixel fallback warning.
- `git diff --check`: passed in both repositories.
- Browser verification and BFF refresh-path simulation were not run because no authenticated disposable admin browser session/backend runtime pair was started.

## Endpoint ownership sheet

| Browser path | Next BFF path | FastAPI path | Read/mutation | Notes |
|---|---|---|---|---|
| Dashboard | `/api/admin/overview` | `GET /api/v1/admin/overview` | Read | Dashboard-specific aggregate projection. |
| Registrations | `/api/admin/registrations` | `GET /api/v1/admin/registrations` | Read | Forwards date/category/page/page_size. |
| Teams | `/api/admin/teams` | `GET /api/v1/admin/teams` | Read | Returns real members and review state. |
| Teams review | `/api/admin/teams/:registrationId/review` | `POST /api/v1/admin/teams/:registrationId/review` | Mutation | Registration-capability roles only; audited. |
| Submissions | `/api/admin/submissions` | `GET /api/v1/admin/submissions` | Read | Shows persisted or truthful Draft state. |
| Evaluators | `/api/admin/evaluators` | `GET /api/v1/admin/evaluators` | Read | Active `JUDGE` users only. |
| Assignment | `/api/admin/submissions/:registrationId/assign` | existing assignment POST | Mutation | Evaluation-capability roles only; no ambiguous retry. |
| Evaluations | `/api/admin/evaluations` | `GET /api/v1/admin/evaluations` | Read | Client derives filter options from response. |
| Audit logs | `/api/admin/logs` | `GET /api/v1/admin/logs` | Read | Optional bounded `limit`; all entries are immutable projections. |

## State ownership sheet

| UI value | Authoritative data | Client rule |
|---|---|---|
| Registration/payment status | Registration plus payment order | Render backend-provided values; do not infer payment from registration status. |
| Registration/team category | Stored member data | Render backend-derived category; filters are query parameters. |
| Team member data/ACM ID | `registration_members` | Render response directly; never generate fixture members. |
| Team review | Registration review fields and audit log | Change only after confirmed review response. |
| Evaluator options/assignment | Active `JUDGE` users and submission record | Fetch options; update only on confirmed assignment response. |
| Evaluations/logs | `evaluations` / `audit_logs` | Render persisted timestamps and values only. |
| Metrics/charts | Overview or filter-consistent list aggregates | Use integer paise and backend aggregate arrays. |

## Out-of-scope guardrail

Do not mark a placeholder route complete by adding static data. Challenge, contacts, announcements, analytics, event control, leaderboard, and settings require their own backend contracts, authorization rules, and implementation plans.
