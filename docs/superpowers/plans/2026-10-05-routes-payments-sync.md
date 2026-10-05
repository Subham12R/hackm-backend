# HackMatrix: finish routes, harden payments, real caching and sync

Repos (branch `claude/dashboard-payment-flow-8ugt7z` in all three):
- `hackm-backend`: FastAPI, SQLAlchemy, Alembic, Postgres.
- `hackm/frontend`: participant site and dashboard (Next 16).
- `admin-hackm`: admin app (Next 16).

## Context

Registration → Razorpay → activation works end to end. Everything after that is unfinished:
- The participant dashboard is read-only. Challenge, timeline, rank, resources and the verification badge are hardcoded. The submission form is disabled, and there is a fake file uploader.
- Seven admin pages are empty stubs.
- Judges can't do anything.
- There is no caching, and nothing refreshes after a change.
- The payment audit found real loopholes (below).

Decisions you made:
- **No file storage.** Submissions are links only:
  - required: repo, pitch deck
  - optional: demo, video, write-up doc, description of up to 1000 characters
- **Remove the uploader.** No S3 or R2 bucket.
- **ACM claims:** admins verify each member's ACM ID. A rejected claim means the team owes the difference, which blocks final submission until it is paid.
- **Caching:** TanStack Query on both frontends, Redis on the backend, and backend performance fixes.
- **Build everything:**
  - event control and deadlines
  - challenge
  - announcements, shown as dashboard notifications with an optional email
  - leaderboard and result release, on the dashboard and a public page
  - judging with multiple rounds and multiple judges
  - contacts inbox
  - admin invites and settings
  - analytics
- **Team edits:** the leader can edit fields that don't change the price. Changes that do change it trigger re-review or an adjustment payment.
- **Refunds:** an admin refund button that goes through Razorpay.

Defaults I'm assuming (tell me if any is wrong):
- A person (email) can be in only one pending or paid team per event.
- Rule dates left empty mean "no restriction", so deploying doesn't close registration.
- Judges see an anonymous code, the project name, the links and the description. They never see team or member identity. GitHub URLs can still reveal usernames; I'll note that in the UI.
- The newsletter stays on Web3Forms.
- Home-page marketing buttons stay unchanged.

## Audit findings → fixes

**Payment (hackm-backend `src/api/registrations.py`):**

| # | Loophole | Fix |
|---|---|---|
| P1 | An empty `RAZORPAY_WEBHOOK_SECRET` means signatures are an HMAC with an empty key, so anyone can forge a `payment.captured` event | Reject with 503 when the secret isn't set |
| P2 | Confirm and webhook can run at the same time with no lock, giving two activation tokens and emails | `SELECT … FOR UPDATE` on the order; insert the webhook event with IntegrityError dedupe |
| P3 | `razorpay_payment_id` is never stored; a captured payment whose amount doesn't match vanishes silently | New `payment_attempts` table with a unique provider id. Mismatches are stored as `NEEDS_REVIEW` and shown to admins |
| P4 | A failed payment lands on `?unconfirmed=1`, and "Pay now" is disabled permanently | Callback detects `error[...]` fields and redirects to `?failed=1` with retry. New `GET /api/v1/registrations/payment-status?order_id=`, which the pay page polls when unconfirmed. Handle `payment.failed` and `order.paid` webhooks |
| P5 | ACM membership is self-declared, and the backend doesn't require an ACM ID | ACM ID required and normalized. Per-member ACM review, plus the adjustment flow below |
| P6 | No input validation, and one person or leader can be in several teams | Validate email, phone (IN), and length limits. Team names are unique case-insensitively. No duplicate emails within a team. Partial unique indexes on leader and member email across pending and paid teams. Drop the unused `amount` field |
| P7 | A leader with several paid teams gets an arbitrary one on the dashboard | Fixed by the unique index from P6; the lookup uses `scalar_one_or_none` |
| P8 | Email is sent inside the database transaction, so a Resend failure means a 500 and a rollback | `email_outbox` table written in the same transaction. A dispatcher sends via BackgroundTasks, and a 60-second in-process retry loop handles failures. Forgot-password still re-sends activation |
| P9 | Errors from `payment.fetch` and `order.create` are unhandled (500) | Map them to 502 with a clear code |
| P10 | No rate limit on `POST /registrations`, so anyone can spam Razorpay orders | IP rate limit using the existing `is_limited` in `src/auth/rate_limits.py` |

**Other backend defects:**
- `create_db_engine` (`src/db/session.py`) builds a new engine and connection pool on every request and never closes it, which will exhaust Neon's connections. Fix: `functools.cache`.
- Login rate limit can be bypassed: the limit is only checked after a failed login. Fix: check it before `authenticate` (`src/api/auth.py` login).
- `POST /admin/evaluations` accepts any role, including VIEWER, and doesn't check assignment. Replaced by the judge API.
- The overview "submissions" metric counts paid registrations. It will count submitted submissions.
- Submission URLs are not validated, so a `javascript:` URL becomes an XSS hole in the admin link column. Fix: https only, and GitHub host check.
- The invite URL is hardcoded to `https://hackmatrix.example`. Fix: use `ADMIN_APP_URL`.
- N+1 queries and pagination done in Python. Fix: add a stored `registrations.category` column, so lists are joined queries with SQL filtering and pagination.
- Pricing tier and the admin category use different rules. Fix: one shared predicate in `src/registrations/pricing.py`.
- Dates are UTC on the server but local time in the browser, so date filters are off by one in India. Fix: the backend buckets by the event timezone (Asia/Kolkata).

**Admin frontend:**
- Session refresh never runs: the access cookie expires with the JWT. Fix: the cookie lasts as long as the refresh token, and the layout gates on the refresh cookie.
- Parallel refreshes sign the user out. Fix: one in-flight refresh at a time in `src/lib/admin-backend.ts`.
- Non-assigner roles can't load the Submissions page because of a 403 on evaluators.
- Empty tables show "Loading…" forever.
- Some row keys collide.
- The overview caption is hardcoded.
- Login shows the wrong message on 429, and the form hangs if the network fails.

**Participant frontend:**
- Logout doesn't revoke the backend session.
- `/session/refresh` can loop forever.
- The BFF routes don't catch network errors.
- The activate button gets stuck.
- Dead code to remove: `mock-payment`, the `/signup` mock (becomes a redirect to `/register`), `participant-dashboard-state.mjs`, `registration-pricing/receipt.ts`, `api/payments/confirm`, `file-upload-1.tsx`.
- Neither BFF has CSRF protection. Fix: an Origin check on non-GET requests. `/api/payments/callback` is exempt because Razorpay POSTs to it cross-origin; it stays signature-verified on the backend.

## Implementation phases

Each phase ends with tests, lint, typecheck and build, an adversarial self-review of the diff, then a commit and push.

### Phase 0: Backend foundations
- `src/db/session.py`: cached engine.
- `src/api/deps.py`: one `require_user(roles)` dependency. It replaces four copy-pasted JWT decoders (`auth.py` super_admin and me, `participant.leader_registration`, `admin.admin_user`).
- `src/cache.py`: Redis JSON get/set with versioned namespaces for invalidation. It does nothing when `REDIS_URL` is unset, so tests and dev need no Redis.
  - Add `redis` and `email-validator` dependencies.
  - Add a `redis:7-alpine` service to `docker-compose.yml`.
- `src/communications/outbox.py`: outbox model and dispatcher, wired into `main.py` startup.

### Phase 1: Payment hardening, ACM adjustment, refunds
The `/registrations`, `/payments/razorpay/confirm` and `/payments/razorpay/webhook` contracts don't change.

**Migration `0007`:**
- `payment_attempts`.
- `refunds`, with a unique provider refund id and a check that refunds never exceed the captured amount.
- `payment_orders.purpose`, either `REGISTRATION` or `ADJUSTMENT`. This drops the unique constraint on `registration_id`; at most one open order per purpose is enforced under a row lock.
- Registrations get `category`, `adjustment_amount_minor`, `adjustment_status` (`NONE|REQUIRED|SETTLED`), `refunded_amount_minor`, and a `REFUNDED` status.
- Members get `acm_review_status` and reviewed by/at.
- Unique index on `lower(team_name)`, plus partial unique indexes on emails. The migration first checks for existing duplicates and aborts with their IDs, rather than corrupting live data.

**Payment handling:**
- `_fulfill_captured` branches on purpose: `REGISTRATION` activates the leader as today; `ADJUSTMENT` sets `adjustment_status=SETTLED` and nothing else.
- Webhook events handled: `payment.captured`, `order.paid`, `payment.failed`, `refund.processed`, `refund.failed`. Duplicates and out-of-order events are safe.

**ACM review and adjustment:**
- Admin: `POST /admin/teams/{id}/members/{mid}/acm-review` (VERIFIED or REJECTED).
- The price is recomputed with rejected members treated as non-ACM. If the new price is higher than what was paid, the team owes the difference and gets an email.
- Participant: `POST /participant/payment-adjustment/order` opens Razorpay checkout for the difference.
- `registration.status` stays `PAID` throughout, so dashboard access is never broken.

**Refunds:**
- `POST /admin/registrations/{id}/refunds`: SUPER_ADMIN only, with an idempotency key, audited.
- A full refund sets status to `REFUNDED`, which removes dashboard access.

**Admin payments visibility:**
- `GET /admin/registrations/{id}`: attempts, orders, refunds, `NEEDS_REVIEW` flags.

### Phase 2: Event control, challenge, announcements, submissions, team edits

**Migration `0008`:**
- `events` gets timestamps for registration, submission, challenge release, and event start/end, plus timezone. All are nullable, and empty means open.
- `challenges` (title, markdown body, resources `[{label,url}]`, version).
- `announcements` and `announcement_reads`.
- Submissions get `pitch_deck_url`, `documentation_url`, `description` (up to 1000 characters), `version`, `updated_at`, and `anonymous_code`.

**Routes:**
- Event: `GET/PATCH /admin/event` and `GET /public/event` (Redis-cached). Registration enforces its window.
- Challenge: `GET/PUT /admin/challenge`. `GET /participant/challenge` returns only `{locked, release_at}` until the release time has passed on the server.
- Announcements:
  - Admin: list, create, edit, publish (optionally emailing leaders through the outbox), delete draft.
  - Participant: `GET /participant/announcements` with an unread flag, and `POST …/read`.
- Submissions:
  - Saves use optimistic `version` checks; a stale save gets 409.
  - Saving and finalizing are checked against the submission window, and finalizing is blocked while an adjustment is owed.
  - Finalizing requires repo and pitch deck, and generates the anonymous code.
  - `POST /admin/submissions/{id}/reopen` is audited.
- Team: `PATCH /participant/team/members/{mid}` edits name, phone, college and ACM ID; emails are locked.
  - Allowed until registration closes.
  - An ACM ID edit resets that member's review.
  - A price-affecting change recomputes the price and can create an adjustment.
  - The audit log records old and new values.

### Phase 3: Judging rounds and results

**Migration `0009`:**
- `evaluation_rounds` (name, sequence, status `DRAFT|OPEN|CLOSED`, minimum evaluations per team).
- `round_entries`: which teams are in a round. Round 1 is every submitted team; later rounds are a shortlist an admin advances.
- `evaluation_criteria` per round (key, label, max, weight).
- `evaluation_assignments` (unique on round, registration and judge).
- `evaluations` (draft or submitted, with comments) and `evaluation_scores`.
- `result_snapshots` (`DRAFT|LOCKED|RELEASED`) and `result_rows`.
- Data migration: existing evaluations and `assigned_user_id` move into "Round 1" with the current 5 criteria.

**Admin routes:**
- Rounds and criteria CRUD.
- Assign or unassign multiple judges.
- `auto-assign` with N judges per submission, balancing load.
- Advance a shortlist to the next round.
- Reopen an evaluation (audited).

**Judge API `/api/v1/judge/*`:**
- Only your own assignments, with anonymous data only.
- Save a draft, then submit once.

**Results:**
- A team's score is the mean over submitted evaluations of the weighted, normalized sum.
- A team counts only once it has the round's minimum number of evaluations.
- Ties are broken by earlier `submitted_at`, then by code.
- Admin actions: generate, then lock, then release. Unlock and unpublish are SUPER_ADMIN only. Everything is audited.
- `GET /public/leaderboard` (Redis, invalidated on release) and `GET /participant/results`. Both return nothing until release.

### Phase 4: Contacts, invites/users/settings, analytics

**Migration `0010`:**
- `contact_messages` and `contact_replies`, where a reply is either a REPLY or a NOTE.

**Routes:**
- Contact:
  - `POST /public/contact`: rate-limited, with a honeypot field.
  - Admin: list, assign, reply (emailed through the outbox), add a note, change status.
- Users and invites:
  - SUPER_ADMIN: list invites, revoke an invite, list admin users, change roles, disable a user.
  - Guards: you can't demote yourself, and there must always be one super admin.
  - Anyone: `POST /auth/password/change`.
- Analytics: `GET /admin/analytics`, cached in Redis for 60s:
  - daily registrations in the event timezone
  - category and college split
  - payment funnel: created, captured, failed, refunded
  - revenue by tier
  - submission rate
  - evaluation progress per round

### Phase 5: Admin frontend (`admin-hackm`)
- TanStack Query provider plus a shared hooks module, `src/lib/queries.ts`, with a key convention. Mutations invalidate related keys; for example, a team review invalidates teams, overview and logs.
- Defaults: `staleTime` 15s, refetch on window focus, and a 30s refetch interval on operational tables.
- Fix the refresh flow and single-flight it, add the Origin check, and fix the page bugs listed above.
- Build the stub pages with the existing shadcn and chart components:
  - Event Control
  - Challenge editor
  - Announcements
  - Leaderboard (rounds, assignments, generate, lock, release)
  - Contacts
  - Analytics
  - Settings (users, invites, password)
- Also:
  - Teams page: per-member ACM verification.
  - Registration detail drawer: payments, refunds, the refund button and `NEEDS_REVIEW` flags.
  - Submissions page: multiple judges per submission and reopen.
  - `/invite?token=` page.
- Judge-only users get a dedicated `/dashboard/judging` area (queue plus scoring form). The sidebar is filtered by role.

### Phase 6: Participant frontend (`hackm/frontend`)
- TanStack Query and shared hooks.
- Dashboard:
  - Real status badge: payment, review, ACM state, adjustment owed (with a "Pay ₹X" checkout).
  - Countdown timeline from event times.
  - Challenge reveal and resources from the API.
  - Editable submission form: save draft, then finalize with a type-SUBMIT confirmation, then a read-only locked state.
  - Team edit dialog.
  - Notification bell with an unread badge in `dashboardnav.tsx`, refetched every 60s.
  - Results section that appears only after release.
- Pay page: `failed` state with retry, and status polling for the `unconfirmed` state.
- Public `/leaderboard` page.
- The contact form posts to the backend.
- Bug fixes and dead-code removal listed above.

## Verification
- **Backend tests:** start local Postgres 16 on :55432, the URL in `tests/integration/*`, then run `uv run pytest`.
- **New tests for each loophole and feature:**
  - a forged webhook with an empty secret
  - confirm and webhook racing, with exactly one activation email
  - a failed payment followed by a retry
  - status polling
  - mismatch leading to `NEEDS_REVIEW`
  - ACM rejection, then an adjustment order, then a captured adjustment that doesn't re-activate the leader
  - refunding more than was captured is rejected, and a duplicate refund is rejected
  - the login limit can't be bypassed
  - a judge can't see unassigned work or identity
  - nothing is visible before release
  - deadline enforcement
  - a stale-version submission save
  - the migration on a database seeded with existing rows
- **Frontends:** `npm run lint`, `npx tsc --noEmit`, `npm run build` for both. The participant app also runs its existing `node --test` suites.
- **Browser check (Playwright with the preinstalled Chromium)** against the local backend with a seeded paid team, admin and judge. Payments are simulated with correctly signed webhooks using a test secret, so no real Razorpay is needed.
  - Participant: dashboard sync and caching after an admin action.
  - Admin: review, assign, score, generate, release.
  - Public leaderboard.
  - Pay page states.

## What you'll need to do at deploy
1. In the Razorpay dashboard, also enable these webhook events: `payment.failed`, `order.paid`, `refund.processed`, `refund.failed`. The webhook URL stays the same.
2. In the backend `.env`, add `REDIS_URL=redis://redis:6379/0`, `ADMIN_APP_URL`, and `EVENT_TIMEZONE=Asia/Kolkata`. Make sure `RAZORPAY_WEBHOOK_SECRET` is set, because the webhook now refuses requests without it.
3. Deploy the backend first; migrations run automatically on container start. Then deploy both frontends.
