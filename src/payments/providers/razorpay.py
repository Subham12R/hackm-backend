import razorpay
from razorpay.errors import SignatureVerificationError


def client(key_id: str, key_secret: str) -> razorpay.Client:
    return razorpay.Client(auth=(key_id, key_secret))


def create_order(api: razorpay.Client, amount_paise: int, receipt: str, notes: dict[str, str] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
        "payment_capture": 1,
    }
    if notes:
        payload["notes"] = notes
    return api.order.create(payload)


def order_payments(api, provider_order_id: str) -> list[dict]:
    """Payments Razorpay holds for an order; used to reconcile when a webhook or callback was lost."""
    orders = getattr(api, "order", None)
    if orders is None or not hasattr(orders, "payments"):
        return []
    response = orders.payments(provider_order_id)
    return list(response.get("items", [])) if isinstance(response, dict) else []


def create_refund(api, provider_payment_id: str, amount_paise: int, receipt: str, notes: dict[str, str]) -> dict[str, object]:
    return api.payment.refund(provider_payment_id, {
        "amount": amount_paise,
        "speed": "normal",
        "receipt": receipt,
        "notes": notes,
    })


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    # An empty secret would make the HMAC forgeable by anyone; refuse instead of "verifying".
    if not secret or not signature:
        return False
    api = razorpay.Client(auth=("unused", "unused"))
    try:
        return bool(api.utility.verify_webhook_signature(body.decode(), signature, secret))
    except (SignatureVerificationError, UnicodeDecodeError):
        return False
