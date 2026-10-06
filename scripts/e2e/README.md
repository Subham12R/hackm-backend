# End-to-end journey

Drives both frontends against a real backend (Postgres + Redis) with a fake Razorpay gateway.
Emails are written to `emails.jsonl` so activation links can be followed.

1. Start Postgres (see `tests/`), Redis on `:6380`, then the backend:
   `PYTHONPATH=. EMAIL_OUTBOX_WORKER=0 uv run uvicorn --app-dir scripts/e2e launcher:app --port 8000`
2. Seed staff with `tests.helpers.staff(...)` and write `staff.json` (`super`, `registration`, `judge`, `judge2`).
3. Build and start both apps in production mode with `BACKEND_URL=http://127.0.0.1:8000`
   (participant on `:3000` with `PUBLIC_APP_URL=http://localhost:3000`, admin on `:3001`).
   Use `localhost`, not `127.0.0.1`: production cookies are `Secure`.
4. `npm i playwright-core` here, then `CHROMIUM_PATH=/path/to/chrome node part1.mjs && node part2.mjs`.

`part1` covers registration, failed-payment retry, lost-callback reconciliation, activation,
submission, member edits, and logout. `part2` covers ACM rejection and balance payment, event control,
challenge reveal, announcements, anonymous judging, result release, the public leaderboard, and refunds.

The `/__e2e/pay` route exists only in `launcher.py`; it is not part of the API.
