from src.communications.providers.sink import LocalEmailSink


def test_local_email_sink_records_a_message_without_sending_network_email() -> None:
    sink = LocalEmailSink()

    sink.send("leader@example.com", "Reset password", "Use this link: https://example.test/reset")

    assert sink.messages == [("leader@example.com", "Reset password", "Use this link: https://example.test/reset")]
