import os

# Tests drive delivery explicitly through request background tasks.
os.environ.setdefault("EMAIL_OUTBOX_WORKER", "0")
