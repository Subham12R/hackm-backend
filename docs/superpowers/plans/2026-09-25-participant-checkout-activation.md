# Participant Checkout and Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A captured Razorpay payment lets only the registered leader set a password and open a participant dashboard backed by the paid registration, with no browser secrets or mock payment.

**Architecture:** FastAPI remains the authority for price, order, webhook fulfilment, activation tokens, and sessions. The participant Next.js app uses same-origin BFF routes to call FastAPI and store only HTTP-only cookies. Razorpay Checkout receives the public key id, order id, and server amount; the browser callback never marks a registration paid.

**Tech Stack:** FastAPI, SQLAlchemy, Razorpay Python SDK, Razorpay Checkout.js, Next.js App Router, pytest, node:test.

**Spec:** `docs/PRD.md`, `CONTEXT.md`

## Global Constraints

- Server price is authoritative. Client `amount` is ignored.
- `payment.captured` plus a matching stored order is the fulfilment gate.
- Activation is single-use, emailed to the registered leader, and also available through a short-lived continuation after verified payment.
- Passwords use Argon2id. Access tokens are Ed25519 JWTs. Refresh tokens stay hashed in PostgreSQL.
- Participant cookies are host-only, `HttpOnly`, `SameSite=Lax`, and `Secure` outside local development.
- `RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET` never reach frontend code or responses except the public `key_id`.
- Do not replace admin routes in this plan.

## Review gate

This slice is not done until all of these are true:

- Backend tests cover activation, replay, wrong-email rejection, and unpaid rejection.
- Participant registration no longer writes a paid receipt to `sessionStorage` or calls `mock-payment`.
- Dashboard unauthenticated access redirects to login.
- A paid test registration can activate and `GET /api/v1/participant/dashboard` returns that team and `PAID` status.
- Review finds no secret in `hackm/frontend`, no client-controlled price, and no payment status derived from the browser callback.

---

### Task 1: Issue and consume an activation token

**Files:**
- Modify: `src/api/registrations.py`
- Modify: `src/db/models/registration.py`
- Create: `migrations/versions/0003_activation_tokens.py`
- Test: `tests/integration/test_activation.py`

**Interfaces:**
- Consumes: paid registration and `PENDING_ACTIVATION` user from the webhook.
- Produces: `POST /api/v1/auth/activate` with `{ "token": str, "password": str }` returning login tokens only after a valid unused token.

- [ ] Write a failing test that pays a registration through the existing webhook fixture, reads the activation token from the email sink, sets a password, and rejects a second use.
- [ ] Run `uv run pytest tests/integration/test_activation.py -q` and confirm the route is missing.
- [ ] Add `activation_tokens(token_hash, user_id, expires_at, consumed_at)`. On `payment.captured`, email one token and return no raw token in the webhook response.
- [ ] Activate by hashing the token, setting the Argon2id password, marking the user `ACTIVE`, granting `PARTICIPANT`, revoking the token, and issuing the existing access/refresh token pair.
- [ ] Re-run the activation test and the registration payment test.

### Task 2: Participant dashboard read model

**Files:**
- Create: `src/api/participant.py`
- Modify: `src/main.py`
- Test: `tests/integration/test_participant_dashboard.py`

**Interfaces:**
- Consumes: bearer access token from Task 1 and the paid registration.
- Produces: `GET /api/v1/participant/dashboard` returning `{ "team_name", "status", "amount_minor", "members": [...] }`.

- [ ] Write a failing test that an unpaid or other leader cannot read the registration, and the activated leader can.
- [ ] Run the test and confirm failure.
- [ ] Implement the route with role and leader-email ownership checks. Do not return payment secrets or webhook payloads.
- [ ] Re-run the test.

### Task 3: Participant BFF and checkout handoff

**Files:**
- Create: `/Users/subham/Dev/hackm/frontend/src/app/api/registrations/route.ts`
- Create: `/Users/subham/Dev/hackm/frontend/src/app/api/registrations/verify/route.ts`
- Create: `/Users/subham/Dev/hackm/frontend/src/app/api/auth/activate/route.ts`
- Modify: `/Users/subham/Dev/hackm/frontend/src/app/register/page.tsx`
- Modify: `/Users/subham/Dev/hackm/frontend/src/app/register/receipt/page.tsx`
- Test: `/Users/subham/Dev/hackm/frontend/src/lib/registration-checkout.test.ts`

**Interfaces:**
- Consumes: Task 1 activation API and the existing registration verify response: `{ order_id, amount, currency, key_id }`.
- Produces: a checkout payload builder that passes only those server fields to Razorpay Checkout.

- [ ] Write a failing node test that the checkout payload uses server `amount` and `order_id`, not a form amount.
- [ ] Run `node --test src/lib/registration-checkout.test.ts` from the frontend and confirm failure.
- [ ] Add BFF routes that call `BACKEND_URL` server-side. The browser never receives the Razorpay secret.
- [ ] After verify, open Razorpay Checkout. The handler may show a pending page only. It must not mark the registration paid.
- [ ] Remove the mock-payment success path from the registration submit flow.
- [ ] Re-run the node test and frontend typecheck.

### Task 4: Activation page and guarded dashboard

**Files:**
- Create: `/Users/subham/Dev/hackm/frontend/src/app/(auth)/activate/page.tsx`
- Modify: `/Users/subham/Dev/hackm/frontend/src/app/dashboard/layout.tsx`
- Modify: `/Users/subham/Dev/hackm/frontend/src/app/dashboard/page.tsx`
- Modify: `/Users/subham/Dev/hackm/frontend/src/lib/leader-auth.ts`

**Interfaces:**
- Consumes: activation BFF and `GET /api/v1/participant/dashboard` through a same-origin BFF.
- Produces: host-only participant cookies and a dashboard render of backend team, status, and amount.

- [ ] Add a server guard that redirects missing or non-participant sessions to `/login`.
- [ ] Activation form posts the emailed token and new password to the BFF, then stores cookies and redirects to `/dashboard`.
- [ ] Dashboard renders backend data and explicit loading, empty, and error states. Keep the existing visual system.
- [ ] Run `npx tsc --noEmit` in `hackm/frontend`.

### Task 5: Review and verification

**Files:**
- Create: `docs/participant-checkout-review.md`
- Modify: `.env.example` only if a new variable name is required.

- [ ] Run `uv run pytest -q`.
- [ ] Run the frontend node test and `npx tsc --noEmit`.
- [ ] Search both repos for `RAZORPAY_KEY_SECRET`, `mock-payment`, and client-side `sessionStorage` payment receipts on the registration path.
- [ ] Record the review result as pass/fail against the review gate. Do not record tokens, cookies, or secrets.

## Deferred

Submission editing, announcements, settings, and admin registration management are separate slices. They start only after this review gate passes.
