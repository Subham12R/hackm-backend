import os

# Tests drive delivery explicitly through request background tasks.
os.environ.setdefault("EMAIL_OUTBOX_WORKER", "0")
os.environ.setdefault("UNPAID_REGISTRATION_WORKER", "0")
os.environ.setdefault("CREDENTIAL_RENDER_WORKER", "0")
# Most tests check business rules at the plain tier prices; fee pass-through has its own tests.
os.environ.setdefault("RAZORPAY_FEE_PERCENT", "0")
