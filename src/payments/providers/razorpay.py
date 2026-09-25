import razorpay
from razorpay.errors import SignatureVerificationError


def client(key_id: str, key_secret: str) -> razorpay.Client:
    return razorpay.Client(auth=(key_id, key_secret))


def create_order(api: razorpay.Client, amount_paise: int, receipt: str) -> dict[str, object]:
    return api.order.create({
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
        "payment_capture": 1,
    })


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    api = razorpay.Client(auth=("unused", "unused"))
    try:
        return bool(api.utility.verify_webhook_signature(body.decode(), signature, secret))
    except SignatureVerificationError:
        return False
