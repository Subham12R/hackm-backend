import resend


from src.communications.senders import from_address, reply_to


class ResendEmailSender:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def send(self, recipient: str, subject: str, html: str, sender: str = "noreply"):
        resend.api_key = self.api_key
        return resend.Emails.send(_params(recipient, subject, html, sender))

    def send_batch(self, messages: list[tuple[str, str, str, str]]) -> list[str | None]:
        """Up to 100 emails in one API call; returns provider ids in the same order."""
        resend.api_key = self.api_key
        response = resend.Batch.send([_params(*message) for message in messages])
        data = response.get("data", []) if isinstance(response, dict) else getattr(response, "data", []) or []
        return [item.get("id") if isinstance(item, dict) else getattr(item, "id", None) for item in data]


def _params(recipient: str, subject: str, html: str, sender: str) -> dict:
    params = {"from": from_address(sender), "to": [recipient], "subject": subject, "html": html}
    if answer_to := reply_to(sender):
        params["reply_to"] = answer_to
    return params
