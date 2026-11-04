"""Transactional email outbox.

Emails are written in the same transaction as the state change that caused them,
then delivered after commit, so a provider outage can never roll back a payment.

Campaign mail carries a `batch_key` and goes out through the provider's batch API (up to
100 emails per call) when the sender supports it; everything else is sent one at a time.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.owner import delivery_address
from src.db.models.registration import EmailOutbox
from src.db.session import create_db_engine

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 6
BATCH_SIZE = 100
# Resend allows a couple of API calls a second; leave room for transactional mail.
BATCH_PAUSE_SECONDS = 0.6


def queue_email(session: Session, recipient: str, subject: str, html: str, sender: str = "noreply", batch_key: str | None = None) -> EmailOutbox:
    """`sender` is one of src.communications.senders.SENDERS: noreply (default), support or announcements."""
    # Mail for the owner account goes to the owner's real inbox (see src/auth/owner.py).
    message = EmailOutbox(recipient=delivery_address(recipient), subject=subject, html=html, sender=sender, status="PENDING", attempts=0,
                          next_attempt_at=datetime.now(UTC), batch_key=batch_key)
    session.add(message)
    return message


def _claim(session: Session, size: int, batched: bool | None):
    query = (
        select(EmailOutbox)
        .where(EmailOutbox.status == "PENDING", EmailOutbox.next_attempt_at <= datetime.now(UTC))
        .order_by(EmailOutbox.created_at)
        .limit(size)
        .with_for_update(skip_locked=True)
    )
    if batched is True:
        query = query.where(EmailOutbox.batch_key.is_not(None))
    elif batched is False:
        query = query.where(EmailOutbox.batch_key.is_(None))
    return list(session.scalars(query))


def _failed(message: EmailOutbox, error: Exception) -> None:
    message.last_error = str(error)[:500]
    if message.attempts >= MAX_ATTEMPTS:
        message.status = "FAILED"
    else:
        message.next_attempt_at = datetime.now(UTC) + timedelta(minutes=2 ** message.attempts)


def _delivered(message: EmailOutbox, provider_id) -> None:
    message.status = "SENT"
    message.sent_at = datetime.now(UTC)
    message.last_error = None
    if provider_id:
        message.provider_message_id = str(provider_id)[:120]


def dispatch_pending(database_url: str, sender, limit: int = 25) -> int:
    """Send due messages, committing each send, so a failure never resends earlier ones.

    Rows are claimed with SKIP LOCKED, so concurrent dispatchers never send the same message.
    """
    sent = 0
    processed = 0
    batching = callable(getattr(sender, "send_batch", None))
    pause = getattr(sender, "batch_pause_seconds", BATCH_PAUSE_SECONDS)
    calls = 0
    with Session(create_db_engine(database_url)) as session:
        if batching:
            while processed < limit:
                messages = _claim(session, min(BATCH_SIZE, limit - processed), batched=True)
                if not messages:
                    session.commit()
                    break
                processed += len(messages)
                for message in messages:
                    message.attempts += 1
                if calls and pause:
                    time.sleep(pause)
                calls += 1
                try:
                    ids = sender.send_batch([(message.recipient, message.subject, message.html, message.sender) for message in messages])
                except Exception as error:
                    for message in messages:
                        _failed(message, error)
                    logger.warning("batch email delivery failed for %d outbox rows", len(messages))
                    session.commit()
                    continue
                for index, message in enumerate(messages):
                    _delivered(message, ids[index] if ids and index < len(ids) else None)
                session.commit()
                sent += len(messages)
        while processed < limit:
            claimed = _claim(session, 1, batched=False if batching else None)
            if not claimed:
                break
            message = claimed[0]
            processed += 1
            message.attempts += 1
            try:
                result = sender.send(message.recipient, message.subject, message.html, sender=message.sender)
            except Exception as error:
                _failed(message, error)
                logger.warning("email delivery failed for outbox %s", message.id)
                session.commit()
                continue
            _delivered(message, result.get("id") if isinstance(result, dict) else None)
            session.commit()
            sent += 1
    return sent
