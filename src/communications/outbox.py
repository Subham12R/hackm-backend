"""Transactional email outbox.

Emails are written in the same transaction as the state change that caused them,
then delivered after commit, so a provider outage can never roll back a payment.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models.registration import EmailOutbox
from src.db.session import create_db_engine

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 6


def queue_email(session: Session, recipient: str, subject: str, html: str) -> EmailOutbox:
    message = EmailOutbox(recipient=recipient, subject=subject, html=html, status="PENDING", attempts=0, next_attempt_at=datetime.now(UTC))
    session.add(message)
    return message


def dispatch_pending(database_url: str, sender, limit: int = 25) -> int:
    """Send due messages; safe to run concurrently because rows are claimed with SKIP LOCKED."""
    sent = 0
    with Session(create_db_engine(database_url)) as session:
        due = session.scalars(
            select(EmailOutbox)
            .where(EmailOutbox.status == "PENDING", EmailOutbox.next_attempt_at <= datetime.now(UTC))
            .order_by(EmailOutbox.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for message in due:
            message.attempts += 1
            try:
                result = sender.send(message.recipient, message.subject, message.html)
            except Exception as error:
                message.last_error = str(error)[:500]
                if message.attempts >= MAX_ATTEMPTS:
                    message.status = "FAILED"
                else:
                    message.next_attempt_at = datetime.now(UTC) + timedelta(minutes=2 ** message.attempts)
                logger.warning("email delivery failed for outbox %s", message.id)
                continue
            message.status = "SENT"
            message.sent_at = datetime.now(UTC)
            if isinstance(result, dict) and result.get("id"):
                message.provider_message_id = str(result["id"])[:120]
            sent += 1
        session.commit()
    return sent
