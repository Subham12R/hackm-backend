import resend


from src.communications.senders import from_address, reply_to


class ResendEmailSender:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def send(self, recipient: str, subject: str, html: str, sender: str = "noreply"):
        resend.api_key = self.api_key
        params = {"from": from_address(sender), "to": [recipient], "subject": subject, "html": html}
        if answer_to := reply_to(sender):
            params["reply_to"] = answer_to
        return resend.Emails.send(params)
