# Contributing to the HackMatrix backend

Thanks for helping. This is the FastAPI and PostgreSQL API behind the participant site and the ORG. admin panel
([`admin-hackm`](https://github.com/Subham12R/admin-hackm)). Small, focused pull requests are the easiest to review.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Contributions are licensed under the
[MIT License](LICENSE).

## Before you start

- **Bug or small fix:** open a PR directly.
- **New feature, schema change, or anything touching payments or auth:** open an issue first so we can agree on the
  approach.
- **Security problem:** do not open an issue. Follow [SECURITY.md](SECURITY.md).

## Local setup

You need Python 3.13, [`uv`](https://docs.astral.sh/uv/), and PostgreSQL 16. The README's "Local setup" section walks
through the database, signing keys, and creating your first admin with `scripts/create_admin.py`.

```sh
uv sync
cp .env.example .env
```

## Running the tests

Integration tests need PostgreSQL at `TEST_DATABASE_URL` (default `127.0.0.1:55432`). They run migrations themselves.

```sh
docker run -d --name hackm-test-db -p 55432:5432 \
  -e POSTGRES_USER=hackm_test -e POSTGRES_PASSWORD=hackm_test -e POSTGRES_DB=hackm_test postgres:16-alpine
uv run pytest
```

Notes for this codebase:

- **API docs are generated and tested.** If you add or change a route, run `uv run python scripts/export_openapi.py`
  and commit the result in `docs/api/`. `tests/unit/test_openapi_docs.py` fails when they drift.
- **Database changes need an Alembic migration** in `migrations/`. Do not edit an existing revision that has shipped.
- **Payments are the source of truth in PostgreSQL.** A browser callback is never proof of payment; keep signature
  checks and webhook deduplication intact. Use Razorpay test-mode keys only.
- **Runtime imports must be main dependencies**, not dev ones. The production image installs with
  `uv sync --no-dev`.
- Never commit `.env`, PEM keys, tokens, or real participant data. `.env.example` holds placeholders only.

## Pull requests

1. Fork the repo and branch from `master` (`fix/short-description` or `feat/short-description`).
2. Keep the change to one concern, and add tests for new behaviour.
3. Use clear commit messages in the imperative mood ("Reject expired refresh tokens"). Conventional Commit prefixes
   (`feat:`, `fix:`, `docs:`) are welcome but not required.
4. Fill in the PR template and link the issue it closes.
5. A maintainer will review. Expect requests for changes; they are about the code, not you.

## Reporting bugs

Use the bug report template. Include what you expected, what happened, the request and response if you have them
(with tokens and personal data removed), and your Python and PostgreSQL versions.
