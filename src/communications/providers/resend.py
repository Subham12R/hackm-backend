import resend


class ResendEmailSender:
    def __init__(self, api_key: str, sender: str) -> None:
        self.api_key = api_key
        self.sender = sender

    def send(self, recipient: str, subject: str, html: str) -> None:
        resend.api_key = self.api_key
        resend.Emails.send({
            "from": self.sender,
            "to": [recipient],
            "subject": subject,
            "html": html,
        })
