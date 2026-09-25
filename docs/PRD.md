# HackMatrix backend-first product requirements document

**Status:** Draft for approval
**Scope:** Full backend for the existing `hackm` participant experience and `admin-hackm` administration experience
**Current backend:** FastAPI in this repository
**Development database:** Neon PostgreSQL
**Later database:** PostgreSQL in Docker on the VPS

This document is the backend contract. The two Next.js repositories are reference implementations for screens, fields, navigation, and seeded examples; their local fixtures, browser storage, and temporary auth are not sources of truth.

## 1. Executive summary

HackMatrix needs one authoritative backend that owns the event, participant registration, team data, account lifecycle, payments, submissions, evaluation, communications, and audit history. Both frontends must read and mutate the same PostgreSQL-backed records through authenticated API routes.

The backend must replace:

- the participant app's environment-only leader credential check;
- the admin app's missing authentication boundary;
- browser `sessionStorage` used as a payment and receipt gate;
- browser-local dashboard and submission state;
- generated admin team-member data;
- client-side Web3Forms submission with an exposed access key;
- hardcoded dashboard metrics, payment statuses, assignments, evaluations, and logs;
- stale references to a future Go backend.

The result is two presentation shells with one data and policy boundary:

```text
hackm participant app ─┐
                       ├── FastAPI API ─── PostgreSQL
admin-hackm app ───────┘
```

No frontend action may claim persistence until the backend has accepted the mutation. No admin or participant page may infer authority from hidden UI controls.

### Implementation baseline

The current backend is FastAPI, so the target implementation uses FastAPI/Pydantic for HTTP contracts, SQLAlchemy 2 with psycopg 3 for PostgreSQL access, Alembic for migrations, Argon2id through a maintained password-hashing library, and `httpx` for provider/email adapters. `uv` remains the project runner. The exact dependency versions belong in `pyproject.toml`; this PRD does not introduce a second backend framework.

## 2. Investigation findings

### 2.1 Participant registration and payment reference

The current participant registration flow is in:

- `hackm/frontend/src/app/register/page.tsx`
- `hackm/frontend/src/components/team-members-form.tsx`
- `hackm/frontend/src/lib/registration-schema.ts`
- `hackm/frontend/src/lib/registration-pricing.ts`
- `hackm/frontend/src/app/register/receipt/page.tsx`
- `hackm/frontend/src/lib/registration-receipt.ts`
- `hackm/frontend/src/lib/mock-payment.ts`
- `hackm/frontend/src/app/(auth)/signup/page.tsx`

The reference fields are:

- team name;
- team leader name, email, phone, college, ACM membership, and conditional ACM ID;
- one to three additional members, each with name, email, phone, college, ACM membership, and conditional ACM ID;
- rules acknowledgement;
- privacy-policy acknowledgement;
- a derived registration price.

The current client enables “Continue to Payment” from the rules checkbox alone even though it renders a separate privacy checkbox. The backend must require both consents, and the frontend must be corrected to reflect that server contract.

The UI therefore represents a two-to-four-person team including the leader. The schema requires at least one additional member but does not encode the UI's maximum; the backend must enforce the configured maximum. The client pricing function currently uses these rules:

- any ACM member from Adamas University: ₹200;
- otherwise any ACM member: ₹300;
- otherwise: ₹400.

The backend must recalculate this quote from normalized submitted members. A client-provided amount, tier, or label is display input only and must never be charged or persisted as authoritative pricing.

The current receipt stores only team name, leader email, and quote in browser session storage. The current payment screen offers UPI, debit/credit card, and netbanking, then simulates success with a timer and puts a second browser-storage object in front of `/signup`. No real payment provider call, order, signature verification, webhook, refund, or payment persistence exists.

### 2.2 Authentication reference

Participant authentication is currently implemented only in:

- `hackm/frontend/src/app/(auth)/login/page.tsx`;
- `hackm/frontend/src/app/api/auth/login/route.ts`;
- `hackm/frontend/src/app/api/auth/logout/route.ts`;
- `hackm/frontend/src/lib/leader-auth.ts`;
- `hackm/frontend/src/app/dashboard/layout.tsx`.

It compares one environment email/password and creates a deterministic HMAC cookie. This is a temporary development gate, not an account system. Forgot-password and reset-password pages do not send anything. Signup fields are disabled and explicitly say the backend is unavailable.

`admin-hackm` has no auth route or protected layout. Its profile dropdown shows a hardcoded user and an inert logout item.

The backend must own account creation, password hashing, session issuance, session revocation, activation, password reset, and role checks for both portals.

### 2.3 Participant dashboard reference

`hackm/frontend/src/app/dashboard/page.tsx` and `participant-dashboard-state.mjs` show the required participant read model:

- authenticated leader identity;
- registration status and message;
- team identity and member roster;
- challenge release state, title, and guidelines link;
- submission status, deadline, project name, GitHub URL, demo URL, and video URL;
- resources;
- leaderboard publication state;
- draft editing and finalization confirmation.

Every displayed status, field, deadline, and member must come from the backend after integration. Finalization must be a transaction guarded by the server deadline and state machine, not a React state change.

### 2.4 Admin dashboard reference

`admin-hackm/src/components/admin-sidebar.tsx` defines these operational areas:

```text
Dashboard
Registrations
Teams
Submissions
Evaluations
Leaderboard
Challenge
Contacts
Announcements
Analytics
Event Control
Audit Logs
Settings
```

The reference pages identify these backend fields and operations:

- **Overview:** registration count, monetary amount, submission count, total users, registration time series, registration source split, recent registrations, and recent logs.
- **Registrations:** registration date, team, leader, email, phone, college, member count, ACM participation/count, category, payment status, date/category filters, server pagination, and selectable columns.
- **Teams:** the same team identity data, expandable member rows, phone, college, ACM status/ID, and a persisted details review state (`Pending`, `Verified`, `Declined`). The current member rows are generated fixtures and must be replaced by stored records.
- **Submissions:** team, project, GitHub/demo/video links, start/end times, status (`Draft`, `Submitted`, `Evaluated`), and persisted evaluator assignment.
- **Evaluations:** anonymous team/evaluation ID, round, Innovation, Technical implementation, Problem solving, UI/UX, Completeness, total score, evaluated time, evaluator, filtering, and sorting. Judge-facing responses must use anonymous projections even though the current admin fixture uses team IDs.
- **Audit Logs:** timestamp, actor, action, resource, and details. Logs must be append-only and generated by backend mutations.
- **Other routes:** Challenge, Contacts, Announcements, Analytics, Event Control, Leaderboard, and Settings are present as placeholders or fixture surfaces and are included in the target backend rather than left as fake local state.

### 2.5 Public communication reference

The participant site currently submits contact messages and newsletter subscriptions directly from the browser through `src/lib/web3forms.ts`. The access key is bundled in client code. The backend must receive, validate, rate-limit, persist, and deliver these messages through a server-side provider/outbox. The key must be removed and rotated before production.

### 2.6 Existing specification alignment

`hackm/docs/superpowers/specs/admin.md` establishes important domain requirements: server-side role enforcement, blind evaluation, anonymous submission codes, auditability, event lifecycle, result locking, and explicit leaderboard release. This PRD carries those requirements into a backend owned by the current FastAPI repository. The current repositories still have separate participant and admin frontends, so this plan uses one API with role-scoped projections rather than pretending they are already one Next application.

### 2.7 Payment-provider investigation and unresolved name

No Razorpay, Lazerpay, Stripe, or other payment SDK/API call exists in either frontend or this backend. The UI's INR pricing and UPI/card/netbanking choices strongly resemble Razorpay Standard Checkout. Razorpay's official integration documentation requires server-side order creation, passing the server-created order ID to checkout, server-side HMAC signature verification, captured-payment confirmation, and signed webhook handling:

- [Razorpay Standard Checkout integration](https://razorpay.com/docs/payments/payment-gateway/web-integration/standard/integration-steps/)
- [Razorpay Standard Checkout guide](https://razorpay.com/docs/developer-tools/integrations/standard-checkout)
- [Razorpay best practices](https://razorpay.com/docs/payments/payment-gateway/web-integration/standard/best-practices/)

If “Laserpay” means **Lazerpay** literally, its official SDK describes a stablecoin gateway with crypto assets and different provider contracts:

- [Lazerpay official Node SDK](https://github.com/LazerpayHQ/lazerpay-node-sdk)
- [Lazerpay official documentation repository](https://github.com/LazerpayHQ/Lazerpay-documentation-v2)

This PRD therefore defines a provider port and uses **Razorpay Standard Checkout as the working assumption**, pending confirmation. Implementation must not silently combine Razorpay and Lazerpay semantics.

## 3. Product goals

1. Persist every business-critical participant and admin action in PostgreSQL.
2. Make payment confirmation and account activation server-authoritative.
3. Give participants a durable dashboard that reflects their own team and event state.
4. Give admins live metrics and operational tables over the same records participants see.
5. Keep payment, password-reset, activation, and email secrets server-side.
6. Enforce roles, permissions, ownership, deadlines, and blind-evaluation projections in the backend.
7. Make Neon PostgreSQL and later Docker PostgreSQL interchangeable through configuration and migrations.
8. Make retries safe for payments, webhooks, email, registration submission, final submission, and administrative actions.

## 4. Non-goals and boundaries

The backend is full-scope for the capabilities represented by the current frontends and the existing HackMatrix admin specification. It is not a generic CRM, LMS, Git host, payment processor, or real-time collaboration platform.

The following are not required for the first implementation unless the event team explicitly adds them:

- OAuth/social login;
- member accounts for every non-leader team member;
- arbitrary multi-event tenant billing;
- binary file hosting; URL-based submission links are enough for the current UI;
- automatic plagiarism detection;
- certificates, attendance QR, sponsors, mentors, or Discord integrations.

Static legal and marketing pages may remain in the frontend, but all event dates, registration availability, pricing, challenge state, announcements, leaderboard state, and operational data must be configurable or queryable from the backend rather than duplicated constants.

## 5. Domain model and vocabulary

- **Event:** one HackMatrix edition. The first deployment has one active event, but event-owned records carry `event_id` so a future edition does not require a schema rewrite.
- **User:** an authenticated identity with one or more roles. A participant leader and an admin are users; a team member may be a stored person without a login.
- **Participant profile:** contact and institution data attached to a participant user.
- **Registration:** a submitted request for one team to enter one event. It records consent, immutable submitted member snapshots, pricing, payment state, and review state.
- **Team:** the operational identity that admins manage and participants own after registration. A team is not the same thing as a payment order or a submission.
- **Team member:** a current team roster row. It may reference a user, but non-leader members do not need accounts in the first release.
- **Quote:** a server-calculated, versioned price for a registration. It is immutable once an order is created.
- **Payment order:** the internal and provider order for a quote. It fixes amount and currency before checkout.
- **Payment attempt:** a provider payment attached to an order. One order may have multiple attempts; only a verified captured attempt fulfils the registration.
- **Activation:** the one-time action that lets a paid team leader create a password and start a participant session.
- **Session:** a revocable, expiring opaque login credential backed by a database row.
- **Submission:** a team's project draft/final snapshot. It has a server-controlled status and an immutable anonymous evaluation code after finalization.
- **Evaluation assignment:** the relationship between a judge and an anonymous submission.
- **Evaluation:** one judge's draft or final rubric response for one assignment.
- **Result snapshot:** a locked calculation over completed evaluations. It is not public until an explicit release action.
- **Audit event:** an append-only record of actor, action, resource, target, timestamp, and relevant state changes.

## 6. Roles, portals, and authorization

### Public visitor

May read public event content, challenge content when released, public announcements, and the leaderboard only after release. May submit a registration, contact request, or newsletter subscription while those features are open.

### Participant team leader

May activate the account created for a paid registration, authenticate through `hackm`, read and update the owned team while allowed, read the challenge and announcements, save/update a submission before the deadline, finalize the submission, and view permitted status/result information.

A participant must never be able to select another team by changing an ID, email, or URL.

### Admin roles

Use capability-based permissions, with these role bundles from the existing admin specification:

```text
SUPER_ADMIN
EVENT_ADMIN
REGISTRATION_ADMIN
EVALUATION_ADMIN
SUPPORT_ADMIN
JUDGE
VIEWER
```

Examples:

- `REGISTRATION_ADMIN`: read/review registrations and teams;
- `EVALUATION_ADMIN`: assign judges, inspect evaluation progress, reopen evaluations, and manage result preparation;
- `SUPPORT_ADMIN`: read and answer contacts;
- `EVENT_ADMIN`: manage event state, challenge, announcements, settings, and permitted results;
- `JUDGE`: read only assigned anonymous submissions, write only owned evaluations, and never receive identity mapping or private aggregate results;
- `VIEWER`: read-only permitted dashboard projections.

The admin frontend remains a separate shell, but a judge is not a separate product or API. Every endpoint checks authentication, permission, resource ownership, and event/result state.

## 7. End-to-end workflows

### 7.1 Registration → payment → activation → participant dashboard

1. Public client loads the active event and registration rules.
2. Visitor enters a team name, leader, one to three members, ACM fields, and both consent checkboxes.
3. Backend normalizes and validates every field, calculates the price from active pricing rules, and creates or updates one active draft for the event + leader email.
4. Email verification is not required before payment. The backend creates or reuses an unpaid Razorpay order from the server-calculated quote; an unpaid order with a changed price is rejected pending support review.
5. The client receives only the public checkout key, provider order ID, amount, currency, and team name. Checkout runs in the provider UI; its callback is not payment proof.
6. Backend verifies the provider signature and captured status against the PostgreSQL-stored order. Webhooks verify raw-body signatures and deduplicate provider events.
7. After confirmed capture, backend emails the registered leader a single-use activation link valid for 30 minutes. The checkout browser goes to `/activate` to prompt the leader to open that email; it never receives an activation token merely for paying. This prevents someone who paid with another person's email from taking over their account.
8. Leader opens the emailed link, sets a password, and receives a participant session. If the 30-minute link expires, the forgot-password form emails a fresh activation link to a pending leader without revealing account existence.
11. ACM ID review is per member and derived at team level. It never blocks activation or ordinary dashboard access; a rejected discount claim creates `PAYMENT_ADJUSTMENT_REQUIRED` and blocks only final submission and result eligibility until settled.
12. Participant dashboard reads the team, member roster, payment/verification state, challenge, submission, deadlines, resources, and leaderboard state from backend APIs.

### 7.2 Login and logout

1. Unauthenticated access to either protected dashboard redirects to its portal login page.
2. Login sends email, password, and expected portal to the backend through the frontend's same-origin BFF route.
3. Backend verifies the password hash, user status, role permission, and event access; it creates a revocable session row.
4. BFF stores the opaque session token in an HTTP-only, secure production cookie. Browser JavaScript never reads it.
5. Protected server components and API routes call the backend with the session token and required portal scope.
6. Logout revokes the current session idempotently and clears the frontend cookie.
7. Password reset revokes all existing sessions after a successful password change.

### 7.3 Forgot password and reset

1. User submits an email from the login page.
2. Backend always returns the same accepted response, whether or not the account exists.
3. For an eligible account, backend stores only a hash of a short-lived single-use reset token and writes an email outbox message.
4. User opens the reset link, submits a new password twice, and backend atomically consumes the token, updates the Argon2id hash, revokes sessions, and writes an audit event.
5. Expired, reused, rate-limited, or malformed tokens fail without revealing account existence.
6. Local development uses a typed email sink/preview; production uses the selected server-side email adapter.

### 7.4 Participant dashboard synchronization

- Dashboard reads are query endpoints, not fixture imports.
- Team edits, submission saves, and finalization return the authoritative record and version/timestamp.
- Draft saves are idempotent and reject stale versions when concurrent edits would overwrite newer data.
- Finalization is allowed only while the event and submission windows are open, then becomes immutable except through an audited admin reopen permission.
- Challenge release, registration status, leaderboard release, and deadlines are enforced server-side.

### 7.5 Admin operations synchronization

- Admin overview counts are database aggregates over the same registrations, captured payments, submissions, users, contacts, and evaluations.
- Registration and team tables use server-side filters, sorting, and pagination.
- Team details read stored member records; no names, phones, ACM IDs, or emails are generated from the team name.
- Payment status comes from payment records, not an email-keyed fixture map.
- Assignment, team review, challenge changes, event controls, announcements, evaluation changes, and result actions persist and produce audit events.

### 7.6 Blind evaluation and release

1. Admin locks eligible submissions and assigns judges.
2. Finalized submissions receive unique non-sequential anonymous codes such as `HM-8K2QP`.
3. Judge endpoints return only assigned anonymous project data and the judge's own evaluation state.
4. Judges can save drafts and submit once; submitted evaluations are locked unless an authorized admin reopens them.
5. The system calculates results only after the configured number of valid evaluations exists.
6. Admin may generate and lock a result snapshot; it remains private.
7. A separately authorized, audited release action makes the public leaderboard available.

### 7.7 Contact and newsletter

- Public contact submissions are validated by the backend, rate-limited, stored, and queued for staff notification.
- Admins can list, assign, respond, note, status-change, and resolve contact threads.
- Newsletter subscriptions are normalized, deduplicated, consented, and stored; delivery is a separate outbox concern.
- The browser must not call Web3Forms or carry an access key.

## 8. Functional requirements

### 8.1 Event and configuration

The backend must expose one active event configuration containing:

- event name, slug, timezone, start/end timestamps;
- registration open/close timestamps;
- submission open/close timestamps;
- challenge release timestamp and current challenge version;
- leaderboard release state;
- team minimum/maximum sizes;
- pricing rules and currency;
- required consents and document versions;
- configured evaluation rounds, criteria, and required judge count;
- public contact and support settings.

The current frontend contains conflicting fixture dates and must not retain authority over them. Event settings are read from the backend.

### 8.2 Registration

Backend validation must include:

- team name present, normalized, length-limited, and unique per event;
- leader plus one to three members;
- every member name, email, phone, and college valid;
- ACM membership explicitly selected;
- ACM ID required when membership is true and normalized when stored;
- no duplicate email within a team or conflicting active team in the event;
- required rules and privacy consent with version/timestamp;
- server-calculated quote and pricing-rule version;
- one active draft per event + leader email, protected by a short-lived emailed resume link;
- immutable draft revisions, where the newest verified revision is authoritative for the next payment order;
- registration creation idempotency key.

The backend must preserve submitted member snapshots and later revisions even if an admin later corrects the operational team roster. Paid leaders may edit before registration closes; a price/ACM-affecting edit recalculates eligibility and can create `PAYMENT_ADJUSTMENT_REQUIRED`.

### 8.3 Pricing

Pricing is configuration-backed, not a frontend constant. The initial rule set mirrors the reference UI:

```text
if any member is ACM and college matches Adamas University → INR 200
else if any member is ACM → INR 300
else → INR 400
```

The rule engine records the matched rule code, amount in minor currency units (paise), currency, and pricing version on the quote. A quote cannot change after provider order creation. If organizers change pricing, new registrations use a new version; old orders retain the old amount.

### 8.4 Payment

The working provider assumption is Razorpay Standard Checkout. The payment domain must remain provider-neutral:

```text
PaymentProvider
├── create_order
├── verify_checkout_result
├── fetch_order
├── fetch_payment
└── create_refund
```

Razorpay adapter requirements:

- create the order on the server using the stored amount/currency/reference;
- never create an order or expose the key secret in browser JavaScript;
- return only public key ID, provider order ID, amount, currency, and safe display metadata;
- verify the checkout HMAC over the server-stored order ID and provider payment ID;
- verify that the returned order belongs to this registration and the expected amount/currency;
- rely on signed webhooks for durable payment state;
- process at least captured, failed, order-paid, refund, and dispute events needed by the event policy;
- store provider IDs and event IDs with unique constraints;
- handle duplicate and out-of-order events safely;
- fulfil/activate only after captured/paid state;
- provide a safe retry/status endpoint when the browser closes before receiving confirmation.

Payment data must distinguish:

```text
provider order
payment attempt
capture state
refund state
webhook delivery state
registration fulfilment state
```

A payment callback that says “success” without a valid signature or captured provider state is not a paid registration.

### 8.5 Account activation and authentication

- Verified captured payment enables only the corresponding leader account for activation; it does not wait for ACM review.
- The activation page is reached immediately through a short-lived HTTP-only continuation and backed up by a separate one-time email link sent to the registered leader email.
- Admin accounts are provisioned by seed/invite, never by public registration.
- Passwords use Argon2id with automatic rehash on parameter change; no plaintext or reversible password storage.
- Access JWTs are Ed25519/EdDSA-signed, include a key ID for rotation, and last 15 minutes. Opaque refresh sessions are hashed in PostgreSQL, rotate on refresh, expire after 30 days, and have a 14-day idle timeout.
- Login, activation, password reset, logout, and session revocation are audited without recording secrets.
- Login and reset endpoints have bounded rate limits and generic account-existence responses.
- Participant and admin frontends may have different login pages and host-only session cookies, but the backend identity/role model is shared.

### 8.6 Participant API/read model

Participant APIs must return only the authenticated leader's permitted event/team data:

- account/profile;
- registration and payment/approval status;
- team and member roster;
- challenge and event deadlines;
- announcements/resources;
- current submission draft/final snapshot;
- participant-visible leaderboard/result state;
- support thread owned by the participant where applicable.

A participant cannot read raw payment provider payloads, internal audit data, other teams, private admin notes, judge identity, or unpublished result data.

### 8.7 Admin API/read model

Admin APIs must support the current navigation and existing admin specification:

- overview metrics and time series;
- registration list, filters, detail, review, approval/rejection, and payment status;
- team list, member detail, verification/decline, and operational notes;
- submission list/detail, lock/reopen policy, links, and judge assignment;
- evaluation list, rubric scores, progress, flags, and result preparation;
- challenge version/release;
- announcements and delivery history;
- contacts, responses, notes, assignment, and statuses;
- analytics derived from persisted records;
- event controls and settings;
- append-only audit log query;
- explicit result lock/unlock/release where role permits.

Every response must be a role-appropriate projection. A judge must never receive team names, member names, emails, colleges, registration IDs, internal team IDs, other judge scores/comments, aggregate score, or private rank.

### 8.8 Submissions

The current participant fields are the minimum initial submission:

- project name;
- GitHub URL;
- demo URL;
- demo video URL.

The model must support a description, track, presentation URL, technologies, notes, versions, timestamps, and server-controlled status without requiring a frontend rewrite.

Statuses:

```text
NOT_STARTED → DRAFT → SUBMITTED → LOCKED
```

An admin reopen is explicit, permission-checked, and audited. Finalization records an immutable snapshot reference and generates the anonymous evaluation code once.

### 8.9 Evaluations and leaderboard

The initial rubric mirrors the admin UI:

- Innovation;
- Technical implementation;
- Problem solving;
- UI/UX;
- Completeness.

Each criterion has configured bounds and a version. Scores, comments, assignments, flags, and submission state are separate records. Aggregation must be deterministic and versioned. Result snapshots freeze source evaluations, criteria, tie-break values, and ranks. Public data appears only after a release action.

### 8.10 Communications

The backend must provide:

- password-reset, resume, and activation email messages;
- contact notification and response email messages;
- announcement targeting and delivery records;
- newsletter subscription storage;
- an outbox with retry count, next-attempt time, terminal failure, and provider message ID.

Resend is the production email adapter. The domain contract must work with a local sink in development and Resend in production.

## 9. API surface

All backend routes are versioned under `/api/v1`. Request validation and response schemas live in the backend; frontend Zod checks are UX only.

### Health and public event

```text
GET  /healthz
GET  /api/v1/public/events/current
GET  /api/v1/public/events/current/challenge
GET  /api/v1/public/events/current/announcements
GET  /api/v1/public/events/current/leaderboard
POST /api/v1/public/newsletter-subscriptions
POST /api/v1/public/contact-messages
```

The public leaderboard returns no rows before an explicit release.

### Authentication

```text
POST /api/v1/auth/login
POST /api/v1/auth/logout
GET  /api/v1/auth/me
POST /api/v1/auth/activate/request
POST /api/v1/auth/activate/complete
POST /api/v1/auth/password/forgot
POST /api/v1/auth/password/reset
POST /api/v1/auth/password/change
GET  /api/v1/auth/sessions
DELETE /api/v1/auth/sessions/{session_id}
```

The frontend apps may proxy these routes through same-origin BFF handlers so server-rendered route guards can work on separate frontend origins. The backend remains the issuer and validator of sessions.

### Registration and payment

```text
POST /api/v1/registrations/quote
POST /api/v1/registrations
GET  /api/v1/registrations/{registration_id}/status
POST /api/v1/registrations/{registration_id}/payment-orders
POST /api/v1/registrations/{registration_id}/payment-confirmations
POST /api/v1/payments/{provider}/webhook
GET  /api/v1/payments/{payment_order_id}/status
POST /api/v1/payments/{payment_order_id}/refunds
```

The public registration status endpoint accepts only a short-lived status token or activation context; it must not expose another team's data by predictable IDs.

### Participant

```text
GET   /api/v1/participant/dashboard
GET   /api/v1/participant/team
PATCH /api/v1/participant/team
GET   /api/v1/participant/challenge
GET   /api/v1/participant/announcements
GET   /api/v1/participant/submission
PUT   /api/v1/participant/submission/draft
POST  /api/v1/participant/submission/finalize
GET   /api/v1/participant/support
POST  /api/v1/participant/support
GET   /api/v1/participant/profile
PATCH /api/v1/participant/profile
```

### Admin

```text
GET   /api/v1/admin/overview
GET   /api/v1/admin/registrations
GET   /api/v1/admin/registrations/{registration_id}
POST  /api/v1/admin/registrations/{registration_id}/approve
POST  /api/v1/admin/registrations/{registration_id}/reject
GET   /api/v1/admin/teams
GET   /api/v1/admin/teams/{team_id}
POST  /api/v1/admin/teams/{team_id}/review
GET   /api/v1/admin/submissions
GET   /api/v1/admin/submissions/{submission_id}
POST  /api/v1/admin/submissions/{submission_id}/assignments
POST  /api/v1/admin/submissions/{submission_id}/reopen
GET   /api/v1/admin/evaluations
POST  /api/v1/admin/evaluations/{evaluation_id}/reopen
GET   /api/v1/admin/leaderboard
POST  /api/v1/admin/leaderboard/generate
POST  /api/v1/admin/leaderboard/lock
POST  /api/v1/admin/leaderboard/release
GET   /api/v1/admin/challenge
PUT   /api/v1/admin/challenge
GET   /api/v1/admin/contacts
POST  /api/v1/admin/contacts/{contact_id}/responses
POST  /api/v1/admin/contacts/{contact_id}/notes
POST  /api/v1/admin/contacts/{contact_id}/status
GET   /api/v1/admin/announcements
POST  /api/v1/admin/announcements
POST  /api/v1/admin/announcements/{announcement_id}/publish
GET   /api/v1/admin/analytics
GET   /api/v1/admin/event-control
PATCH /api/v1/admin/event-control
GET   /api/v1/admin/audit-logs
GET   /api/v1/admin/settings
PATCH /api/v1/admin/settings
```

A future judge UI may use the shared submission/evaluation resources with a judge projection; it must not receive an admin projection by changing a URL.

## 10. PostgreSQL data model

### 10.1 Database conventions

- PostgreSQL is the source of truth; no SQLite fallback.
- Use UUID primary keys for internal rows and provider-generated IDs only in provider columns.
- Store all timestamps as `timestamptz` in UTC.
- Store money as integer minor units plus a three-letter currency code.
- Store normalized email separately or in a consistently normalized column with a unique constraint.
- Use explicit status/check constraints and foreign keys; do not rely only on Pydantic validation.
- Use `created_at`, `updated_at`, and immutable event timestamps where appropriate.
- Encrypt or tightly restrict high-sensitivity PII at rest where deployment policy requires it; never return it to a role that does not need it.

### 10.2 Identity and authentication tables

#### `users`

```text
id UUID PK
email_normalized TEXT UNIQUE NOT NULL
display_name TEXT NOT NULL
password_hash TEXT NULL
status ENUM: INVITED, PENDING_EMAIL_VERIFICATION, PENDING_PAYMENT, PENDING_ACTIVATION, ACTIVE, SUSPENDED, DISABLED
last_login_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

A pending participant may exist before activation, but cannot log in. Admin users are created by seed/invite.

#### `roles`

```text
id UUID PK
code TEXT UNIQUE NOT NULL
name TEXT NOT NULL
```

#### `user_roles`

```text
user_id UUID FK users
role_id UUID FK roles
created_at TIMESTAMPTZ NOT NULL
PRIMARY KEY (user_id, role_id)
```

#### `participant_profiles`

```text
id UUID PK
user_id UUID UNIQUE FK users
full_name TEXT NOT NULL
phone TEXT NOT NULL
college TEXT NOT NULL
acm_member BOOLEAN NOT NULL
acm_id TEXT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

#### `sessions`

```text
id UUID PK
user_id UUID FK users
token_hash TEXT UNIQUE NOT NULL
expires_at TIMESTAMPTZ NOT NULL
revoked_at TIMESTAMPTZ NULL
last_seen_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
ip_hash TEXT NULL
user_agent_hash TEXT NULL
```

Raw tokens are never stored. Expired/revoked rows remain for audit/cleanup rather than being silently reused.

#### `account_activation_tokens`

```text
id UUID PK
user_id UUID FK users
registration_id UUID FK registrations
token_hash TEXT UNIQUE NOT NULL
expires_at TIMESTAMPTZ NOT NULL
consumed_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
```

#### `password_reset_tokens`

```text
id UUID PK
user_id UUID FK users
token_hash TEXT UNIQUE NOT NULL
expires_at TIMESTAMPTZ NOT NULL
attempt_count INTEGER NOT NULL
consumed_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
```

### 10.3 Event and configuration tables

#### `events`

```text
id UUID PK
slug TEXT UNIQUE NOT NULL
name TEXT NOT NULL
timezone TEXT NOT NULL
status ENUM: DRAFT, REGISTRATION_OPEN, REGISTRATION_CLOSED, LIVE, SUBMISSION_CLOSED, JUDGING, RESULTS_LOCKED, PUBLIC, ARCHIVED
starts_at TIMESTAMPTZ NULL
ends_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

#### `event_settings`

```text
event_id UUID PK/FK events
registration_opens_at TIMESTAMPTZ
registration_closes_at TIMESTAMPTZ
submission_opens_at TIMESTAMPTZ
submission_deadline TIMESTAMPTZ
team_min_size SMALLINT
team_max_size SMALLINT
currency CHAR(3)
required_judge_count SMALLINT
leaderboard_released_at TIMESTAMPTZ NULL
settings_version INTEGER NOT NULL
updated_by UUID FK users
updated_at TIMESTAMPTZ NOT NULL
```

#### `pricing_rules`

```text
id UUID PK
event_id UUID FK events
code TEXT NOT NULL
priority INTEGER NOT NULL
amount_minor INTEGER NOT NULL
currency CHAR(3) NOT NULL
condition_json JSONB NOT NULL
version INTEGER NOT NULL
active BOOLEAN NOT NULL
UNIQUE (event_id, code, version)
```

#### `challenges` and `challenge_versions`

The challenge has an event-owned identity and immutable published versions. A version contains title, statement, rules, requirements, resources, judging criteria reference, and release timestamp. Only a released version is visible to participants/public users.

#### `announcements` and `announcement_deliveries`

Announcements contain title, body, audience, priority, scheduled/published timestamps, author, and status. Delivery rows track each channel/recipient batch without making delivery the source of truth.

### 10.4 Registration and teams

#### `teams`

```text
id UUID PK
event_id UUID FK events
name TEXT NOT NULL
name_normalized TEXT NOT NULL
leader_user_id UUID NULL FK users
status ENUM: DRAFT, PAYMENT_PENDING, ACTIVE, PAYMENT_ADJUSTMENT_REQUIRED, FLAGGED, SUSPENDED, CANCELLED
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
UNIQUE (event_id, name_normalized)
```

#### `registrations`

```text
id UUID PK
event_id UUID FK events
team_id UUID UNIQUE FK teams
leader_member_id UUID NULL
status ENUM: DRAFT, EMAIL_VERIFIED, PAYMENT_PENDING, PAID, PAYMENT_ADJUSTMENT_REQUIRED, CANCELLED, SUSPENDED
rules_consent_version TEXT NOT NULL
rules_consented_at TIMESTAMPTZ NOT NULL
privacy_consent_version TEXT NOT NULL
privacy_consented_at TIMESTAMPTZ NOT NULL
quoted_amount_minor INTEGER NOT NULL
quoted_currency CHAR(3) NOT NULL
pricing_rule_code TEXT NOT NULL
pricing_version INTEGER NOT NULL
submitted_at TIMESTAMPTZ NOT NULL
reviewed_at TIMESTAMPTZ NULL
reviewed_by UUID NULL FK users
rejection_reason TEXT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

#### `registration_member_snapshots`

```text
id UUID PK
registration_id UUID FK registrations
member_role ENUM: LEADER, MEMBER
full_name TEXT NOT NULL
email_normalized TEXT NOT NULL
phone TEXT NOT NULL
college TEXT NOT NULL
acm_member BOOLEAN NOT NULL
acm_id TEXT NULL
position SMALLINT NOT NULL
created_at TIMESTAMPTZ NOT NULL
UNIQUE (registration_id, position)
```

This is the immutable submitted payload used for audit and payment/pricing decisions.

#### `team_members`

```text
id UUID PK
team_id UUID FK teams
user_id UUID NULL FK users
member_role ENUM: LEADER, MEMBER
full_name TEXT NOT NULL
email_normalized TEXT NOT NULL
phone TEXT NOT NULL
college TEXT NOT NULL
acm_member BOOLEAN NOT NULL
acm_id TEXT NULL
review_status ENUM: PENDING, VERIFIED, REJECTED
joined_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
UNIQUE (team_id, email_normalized)
```

Operational corrections do not rewrite the registration snapshot. A leader's `user_id` is linked after activation.

### 10.5 Payments

#### `payment_orders`

```text
id UUID PK
registration_id UUID FK registrations
provider TEXT NOT NULL
provider_order_id TEXT UNIQUE NOT NULL
internal_reference TEXT UNIQUE NOT NULL
amount_minor INTEGER NOT NULL
currency CHAR(3) NOT NULL
status ENUM: CREATED, ATTEMPTED, PAID, FAILED, CANCELLED, REFUNDED, DISPUTED
provider_payload JSONB NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

#### `payment_attempts`

```text
id UUID PK
payment_order_id UUID FK payment_orders
provider_payment_id TEXT UNIQUE NOT NULL
method TEXT NULL
status ENUM: CREATED, AUTHORIZED, CAPTURED, FAILED, REFUNDED, DISPUTED
signature_verified BOOLEAN NOT NULL
amount_minor INTEGER NOT NULL
currency CHAR(3) NOT NULL
captured_at TIMESTAMPTZ NULL
failure_code TEXT NULL
provider_payload JSONB NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

#### `payment_webhook_events`

```text
id UUID PK
provider TEXT NOT NULL
provider_event_id TEXT NOT NULL
signature_verified BOOLEAN NOT NULL
received_at TIMESTAMPTZ NOT NULL
processed_at TIMESTAMPTZ NULL
status ENUM: RECEIVED, PROCESSED, IGNORED, FAILED
attempt_count INTEGER NOT NULL
payload JSONB NOT NULL
last_error TEXT NULL
UNIQUE (provider, provider_event_id)
```

#### `refunds`

```text
id UUID PK
payment_attempt_id UUID FK payment_attempts
provider_refund_id TEXT UNIQUE NOT NULL
amount_minor INTEGER NOT NULL
currency CHAR(3) NOT NULL
status ENUM: CREATED, PROCESSED, FAILED
reason TEXT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Provider payloads must be redacted to exclude secrets and unnecessary payment-card data. The database stores provider references and verification facts, not card details.

### 10.6 Submissions and evaluation

#### `submissions`

```text
id UUID PK
event_id UUID FK events
team_id UUID FK teams
evaluation_code TEXT UNIQUE NULL
status ENUM: NOT_STARTED, DRAFT, SUBMITTED, LOCKED, REOPENED
current_version_id UUID NULL
submitted_at TIMESTAMPTZ NULL
locked_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

#### `submission_versions`

```text
id UUID PK
submission_id UUID FK submissions
version_number INTEGER NOT NULL
project_name TEXT NOT NULL
description TEXT NULL
track TEXT NULL
repository_url TEXT NULL
demo_url TEXT NULL
video_url TEXT NULL
presentation_url TEXT NULL
technologies JSONB NULL
notes TEXT NULL
created_by UUID FK users
created_at TIMESTAMPTZ NOT NULL
UNIQUE (submission_id, version_number)
```

#### `evaluation_criteria`

```text
id UUID PK
event_id UUID FK events
code TEXT NOT NULL
label TEXT NOT NULL
max_score NUMERIC NOT NULL
sort_order INTEGER NOT NULL
version INTEGER NOT NULL
active BOOLEAN NOT NULL
```

The first criteria are innovation, technical implementation, problem solving, UI/UX, and completeness.

#### `judge_profiles`

Stores judge-specific expertise, conflict-of-interest status, and operational metadata attached to a `JUDGE` user. It is not a separate authentication identity.

#### `evaluation_assignments`

```text
id UUID PK
submission_id UUID FK submissions
judge_user_id UUID FK users
round TEXT NOT NULL
status ENUM: ASSIGNED, IN_PROGRESS, SUBMITTED, REOPENED, WITHDRAWN
assigned_at TIMESTAMPTZ NOT NULL
completed_at TIMESTAMPTZ NULL
UNIQUE (submission_id, judge_user_id, round)
```

#### `evaluations` and `evaluation_scores`

An evaluation belongs to one assignment. Scores are one row per criterion with bounds validated by the criterion version. Submitted evaluations are immutable unless an authorized reopen creates an audit event and transitions the assignment/evaluation state.

#### `evaluation_flags`

Stores judge issues such as broken links, conflict of interest, plagiarism concern, or integrity review, with visibility and resolution state.

#### `result_snapshots`, `result_rows`, and `result_releases`

A snapshot stores the exact calculation inputs/version and locked output. Rows contain anonymous code, team mapping for authorized admin views, score components, tie-break data, and rank. A release row records who released which snapshot, when, and the public publication state.

### 10.7 Communications and audit

#### `contact_messages`, `contact_responses`, and `contact_notes`

Store the original public message, category, status, assignee, responses, internal notes, timestamps, and email-delivery state. Internal notes are never returned to participants.

#### `newsletter_subscribers`

```text
id UUID PK
email_normalized TEXT UNIQUE NOT NULL
status ENUM: SUBSCRIBED, UNSUBSCRIBED, BOUNCED
source TEXT NULL
consented_at TIMESTAMPTZ NOT NULL
unsubscribed_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

#### `email_outbox` and `email_deliveries`

The outbox stores template, recipient, safe metadata, retry state, and idempotency key. Delivery rows store provider ID, status, and timestamps. Password and activation token values are never stored in plaintext in delivery records or logs.

#### `audit_logs`

```text
id UUID PK
event_id UUID NULL FK events
actor_user_id UUID NULL FK users
action TEXT NOT NULL
resource_type TEXT NOT NULL
resource_id UUID NULL
before_json JSONB NULL
after_json JSONB NULL
request_id TEXT NULL
created_at TIMESTAMPTZ NOT NULL
```

Audit rows are append-only. Normal admin APIs cannot edit or delete them.

#### `idempotency_keys`

Stores endpoint scope, caller, key, request fingerprint, result reference, and expiry for registration creation, payment-order creation, payment confirmation, webhook processing, final submission, and other high-impact mutations.

## 11. State transitions and invariants

### Registration

```text
DRAFT → EMAIL_VERIFIED → PAYMENT_PENDING → PAID
                                           └──→ PAYMENT_ADJUSTMENT_REQUIRED → PAID
DRAFT/PAYMENT_PENDING → CANCELLED
PAID → SUSPENDED (documented exception only)
```

Payment verification and per-member ACM review are separate state machines. A verified captured payment enables activation; ordinary ACM review does not require a global team approval.

### Payment

```text
CREATED → ATTEMPTED → AUTHORIZED → CAPTURED → PAID
                         └────────→ FAILED
CAPTURED/PAID → REFUNDED or DISPUTED
```

Provider events may arrive repeatedly or out of order; the service applies only valid monotonic transitions and records ignored events.

### Team review

```text
PENDING → VERIFIED
PENDING → DECLINED
VERIFIED → LOCKED
```

### Submission

```text
NOT_STARTED → DRAFT → SUBMITTED → LOCKED
                         ↑          │
                         └─ REOPENED┘ (authorized and audited)
```

### Evaluation/results

```text
ASSIGNED → IN_PROGRESS → SUBMITTED
                         └→ REOPENED → SUBMITTED

EVALUATING → EVALUATION_COMPLETE → RESULTS_GENERATED → RESULTS_LOCKED → PUBLIC
```

No invalid transition is accepted because a button was clicked twice or a stale page was submitted.

## 12. Security, privacy, and reliability

- Argon2id password hashes; no plaintext passwords.
- HTTP-only, secure production cookies; SameSite policy and origin checks for state-changing BFF requests.
- Opaque session tokens hashed at rest; expiry, revocation, and password-reset invalidation.
- Generic login/reset responses to prevent account enumeration.
- Bounded login, reset, contact, registration, submission, and evaluation rate limits.
- Backend validation is authoritative; frontend validation is advisory.
- SQL parameterization/ORM binding and database constraints protect invariants.
- Payment signatures and raw webhook bodies are verified server-side; webhook processing is idempotent.
- No card data, provider secret, email access key, password, token, OTP, or full private message in logs.
- Request IDs and structured redacted logs for all API errors and high-impact mutations.
- Role and ownership checks on every object read/write; no insecure direct object references.
- Judge projections exclude identity at serialization time, not by frontend filtering.
- Critical actions require explicit confirmation and append an audit log.
- PostgreSQL backups and restore checks are required before event-critical milestones.
- Bounded retries with terminal failure states for email, webhooks, and provider calls; no unbounded retry loop.

## 13. Environment and deployment contract

The code must use a provider-neutral PostgreSQL URL:

```text
DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:<port>/<database>?sslmode=require
```

Neon is the development provider. The later VPS deployment changes `DATABASE_URL` to the Docker PostgreSQL service/credentials and runs the same Alembic migrations. No schema fork or SQLite fallback is allowed.

Required configuration categories:

```text
DATABASE_URL
SESSION_SECRET
SESSION_TTL_SECONDS
COOKIE_SECURE
FRONTEND_ORIGINS
EMAIL_MODE
EMAIL_FROM
RESEND_API_KEY
PAYMENT_PROVIDER=razorpay
RAZORPAY_KEY_ID
RAZORPAY_KEY_SECRET
RAZORPAY_WEBHOOK_SECRET
```

Only public payment key IDs may reach the checkout browser. All secrets live in ignored environment/secret-manager configuration.

Local development requires:

- an idempotent seed command for one admin, one paid participant/team, one unpaid/failed-payment registration, judges, sample submissions, and sample logs;
- a typed payment-provider fixture for unit/integration tests;
- a typed email sink that lets developers inspect activation/reset messages without sending real email;
- a Neon development database or disposable PostgreSQL instance for integration tests.

## 14. Testing and acceptance

### Unit tests

- registration field validation and duplicate detection;
- price-rule calculation and versioning;
- password hashing and token hashing;
- permission and ownership policies;
- valid/invalid status transitions;
- payment signature verification;
- webhook event deduplication and monotonic transitions;
- evaluation scoring and result calculation;
- redacted role projections.

### Integration tests

- migration from an empty PostgreSQL database;
- idempotent seed;
- registration transaction and quote persistence;
- provider order creation with server-owned amount;
- payment confirmation and captured webhook;
- failed/refunded payment not activating an account;
- activation, login, `me`, logout, expiry, and password reset;
- participant dashboard data after admin approval;
- participant submission save/finalize/deadline rejection;
- admin filters, review, assignment, and audit rows;
- judge identity isolation and result-release gating;
- contact/newsletter persistence and outbox creation.

### Browser end-to-end tests

1. Public visitor completes the registration form with valid leader and member data.
2. Backend returns the calculated quote and provider order; client cannot alter the amount.
3. Test provider success plus signed webhook changes the registration to paid.
4. Activation link creates a participant password and session.
5. Participant login/reload/logout works and dashboard reflects the seeded/live team.
6. Participant saves and finalizes a submission; admin sees the same submission/status.
7. Admin login/reload/logout works and dashboard counts/registration/payment state reflect database records.
8. Admin reviews the team and assigns a judge; the assignment persists after reload.
9. Forgot-password request, reset link, password change, session revocation, and re-login work.
10. Wrong-role, expired-session, duplicate-webhook, failed-payment, provider outage, and deadline cases show explicit errors and no false success.

Live payment E2E uses provider test mode only after the provider decision and credentials are supplied. Without external credentials, the backend test-provider fixture must verify the same typed order/signature/webhook contract; it must never be enabled in production.

### Completion definition

The backend slice is complete only when both frontends stop reading the corresponding fixture/local state and the same PostgreSQL records are observable through:

```text
registration → payment → activation → login → participant dashboard
                                               ↓
                                      admin dashboard views

participant submission → admin submission/evaluation views
admin review/assignment → participant-visible state where permitted
```

## 15. Implementation phases

### Phase 0 — contract and infrastructure

- add typed settings, dependency lock updates, Alembic, PostgreSQL session management, error envelope, request IDs, and health checks;
- create the schema and idempotent seed;
- create payment/email ports and local test adapters.

### Phase 1 — secure authentication foundation

- implement Neon migrations, Argon2id users/roles, EdDSA JWT access tokens, PostgreSQL-backed rotating refresh sessions, admin bootstrap/invites, password reset, Resend/local-sink email adapter, and participant/admin BFF login/logout guards;
- replace the temporary participant env auth and the unauthenticated admin dashboard only after Phase 1 tests pass;
- do not connect Razorpay or public account activation in this phase.

### Phase 2 — registration, Razorpay, and activation

- implement server validation, email-verified draft resume/revisions, pricing, team/member records, Razorpay orders/signatures/webhooks, payment adjustments/refunds, activation continuation/email links, and participant payment/verification state;
- replace browser receipt/mock-payment/session-storage flow only after provider fixture and webhook tests pass.

### Phase 3 — live participant and admin operational data

- replace participant dashboard fixtures/local mutations;
- replace admin identity, metrics, registrations, teams, payment status, submissions, assignments, and logs;
- add server pagination, participant/admin projections, and audit views.

### Phase 4 — event operations

- challenge, announcements, event control, contacts, newsletter, Resend delivery history, settings, analytics, and audit views.

### Phase 5 — judging and results

- anonymous submission codes, judge assignments, blind projections, rubric/evaluation locking, result snapshots, leaderboard release, and public results.

### Phase 6 — deployment hardening

- Neon migration rehearsal and backup/restore test;
- Docker PostgreSQL VPS deployment with the same migrations/config contract;
- provider live-mode webhook verification, email delivery verification, rate-limit tuning, and production security review.

## 16. Settled implementation decisions

- Payment provider: **Razorpay Standard Checkout**. `payment.captured` is the fulfilment gate; `order.paid` is recorded as supporting state.
- Email provider: **Resend** in production and a local typed sink in development.
- Authentication: Argon2id passwords, Ed25519/EdDSA 15-minute access JWTs, and rotating PostgreSQL refresh sessions.
- Account model: only team leaders receive accounts in the first release; other members are stored team records.
- Payment/activation: payment verification redirects immediately through a protected activation continuation and separately emails a one-time recovery activation link.
- ACM review: manual, per member, ID-first, and non-blocking; evidence collection happens outside the app in v1. A rejected discount claim causes payment adjustment, not automatic refund.
- Refunds: only authorized Event/Super Admins can create audited manual Razorpay refunds.
- Active registration: one active event is exposed in UI while all event-owned records retain `event_id`.
- Event dates/timezone: configured in backend event settings; current fixture dates are not authoritative.
