import hashlib
import hmac

from src.payments.providers.razorpay import verify_webhook_signature


def test_webhook_signature_accepts_only_the_exact_raw_body() -> None:
    body = b'{"event":"payment.captured"}'
    secret = "webhook-secret"
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(body, signature, secret)
    assert not verify_webhook_signature(body + b" ", signature, secret)
    assert not verify_webhook_signature(body, signature, "other-secret")
