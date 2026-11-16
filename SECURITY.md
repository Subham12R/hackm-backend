# Security Policy

## Supported versions

Only the latest commit on `master` is supported. Fixes are not backported.

## Reporting a vulnerability

**Please do not open a public issue for security problems.** This API handles identity, sessions, and Razorpay payment
confirmation, so private reports matter.

Report privately in either way:

1. Use GitHub's private reporting: open the **Security** tab of this repository and choose **Report a vulnerability**.
2. Or email **dev@subham12r.me** with the subject `SECURITY: hackm-backend`.

Include what you found, how to reproduce it, the affected routes or modules, and the impact you see. Do not include
real participant data, live credentials, or payment details.

You can expect an acknowledgement within 3 working days and a first assessment within 7 days. We will keep you
updated, and credit you in the fix unless you prefer to stay anonymous.

## Scope

In scope: authentication and session handling (JWTs, refresh rotation), role and policy checks, payment confirmation
and webhook verification, registration pricing, rate limiting, file upload paths, and injection or data exposure bugs.

Please do not run automated scans against a live production deployment, access data that is not yours, test payment
flows with real money, or disrupt events that are running.

## For operators

If you self-host: keep `.env` out of version control, generate your own Ed25519 signing keys, use Razorpay test-mode
keys until you go live, and set `RAZORPAY_WEBHOOK_SECRET` (webhooks are refused without it).
