"""E2E backend: real app + Postgres + Redis, fake Razorpay, emails written to a JSON-lines file."""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6380/0")
os.environ["PARTICIPANT_APP_URL"] = "http://127.0.0.1:3000"
os.environ["ADMIN_APP_URL"] = "http://127.0.0.1:3001"

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi import Request

from src.main import create_app
from tests.helpers import FakeRazorpay, TEST_DATABASE_URL, migrate


class FileSink:
    def send(self, recipient, subject, html):
        with open(os.path.join(os.environ.get("E2E_DIR", os.path.dirname(os.path.abspath(__file__))), "emails.jsonl"), "a") as handle:
            handle.write(json.dumps({"to": recipient, "subject": subject, "html": html}) + "\n")


migrate()
key = Ed25519PrivateKey.generate()
gateway = FakeRazorpay()
app = create_app(
    database_url=TEST_DATABASE_URL, email_sender=FileSink(), payment_client=gateway,
    razorpay_webhook_secret="webhook-secret", razorpay_key_id="rzp_test_e2e",
    jwt_private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
    jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
)


@app.post("/__e2e/pay")
async def e2e_pay(request: Request):
    """Simulates Razorpay capturing (or failing) a payment on an order."""
    body = await request.json()
    payment = gateway.pay(body["order_id"], body["amount"], status=body.get("status", "captured"))
    return payment
