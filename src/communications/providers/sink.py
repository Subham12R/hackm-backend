class LocalEmailSink:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send(self, recipient: str, subject: str, html: str) -> None:
        self.messages.append((recipient, subject, html))
