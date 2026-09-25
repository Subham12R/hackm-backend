# Participant checkout review

Date: 2026-09-25

## Goal

A captured Razorpay payment lets only the registered leader set a password and open a dashboard backed by that paid registration.

## Result

Pass for the backend contract. Partial for the participant UI.

## Evidence

- `uv run pytest -q` in `hackm-backend`: 26 passed.
- `node --test src/lib/registration-checkout.test.ts` in `hackm/frontend`: 1 passed.
- `npx tsc --noEmit` reports no errors in the changed app files. Existing `.ts` test imports still fail typecheck, including the pre-existing pricing test.

## Gate

- Activation token is single-use, emailed on `payment.captured`, and absent from the webhook response. Pass.
- Registration submit no longer saves a mock payment or client price as payment proof. Pass.
- Checkout payload uses server `order_id`, `amount`, and public `key_id` only. Pass.
- Dashboard layout requires a backend-validated participant access cookie. Pass.
- Dashboard shows backend team, status, and amount. Pass.
- Submission editor still uses the local fixture and is not a server submission. Fail for the full dashboard scope, deferred.
- Live local check passed: registration 202, email verify 200, Razorpay test checkout captured ₹200, signed webhook 200, activation 200, replay 400, dashboard 200 with `PAID` and 2 members. Tokens were not recorded.

## Required local config

`hackm/frontend` needs `BACKEND_URL=http://127.0.0.1:8000`. Do not add Razorpay secrets there.
