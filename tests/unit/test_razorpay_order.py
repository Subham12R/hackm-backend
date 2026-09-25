from src.payments.providers.razorpay import create_order


class FakeOrders:
    def __init__(self) -> None:
        self.payload = None

    def create(self, payload):
        self.payload = payload
        return {"id": "order_test", "amount": payload["amount"], "currency": "INR"}


class FakeClient:
    def __init__(self) -> None:
        self.order = FakeOrders()


def test_order_uses_server_paise_amount_and_inr() -> None:
    api = FakeClient()

    order = create_order(api, 20000, "reg_123")

    assert order["id"] == "order_test"
    assert api.order.payload == {
        "amount": 20000,
        "currency": "INR",
        "receipt": "reg_123",
        "payment_capture": 1,
    }
