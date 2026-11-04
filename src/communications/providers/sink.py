class LocalEmailSink:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []
        # Parallel to `messages`: which sender each one used.
        self.senders: list[str] = []

    def send(self, recipient: str, subject: str, html: str, sender: str = "noreply") -> None:
        self.messages.append((recipient, subject, html))
        self.senders.append(sender)

    # Tests and local dev: no provider rate limit to respect between batches.
    batch_pause_seconds = 0
    # How many batch calls were made, so tests can see campaign mail used the batch path.
    batch_calls = 0

    def send_batch(self, messages: list[tuple[str, str, str, str]]) -> list[str]:
        self.batch_calls += 1
        for recipient, subject, html, sender in messages:
            self.send(recipient, subject, html, sender=sender)
        return [f"sink-{len(self.messages) - len(messages) + index}" for index in range(len(messages))]
