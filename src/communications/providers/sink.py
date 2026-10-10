class LocalEmailSink:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []
        # Parallel to `messages`: which sender each one used.
        self.senders: list[str] = []

    def send(self, recipient: str, subject: str, html: str, sender: str = "noreply") -> None:
        self.messages.append((recipient, subject, html))
        self.senders.append(sender)
