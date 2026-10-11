"""Unpaid registrations: one reminder after REMIND_AFTER, then they lapse LAPSE_AFTER_REMINDER later.

A lapsed registration is ABANDONED with its orders cancelled, which frees the team name and member
emails. A payment that still lands on a cancelled order is recorded for review, never lost.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.communications.outbox import queue_email
from src.communications.templates import action_email
from src.db.models.registration import PaymentOrder, Registration
from src.db.session import create_db_engine
from src.payments import service as payments

logger = logging.getLogger(__name__)
REMIND_AFTER = timedelta(hours=1)
LAPSE_AFTER_REMINDER = timedelta(hours=1)


def sweep_unpaid(database_url: str, api, now: datetime | None = None) -> tuple[int, int]:
    """Queue due reminders and lapse registrations whose reminder hour is up. Returns (reminded, lapsed)."""
    now = now or datetime.now(UTC)
    reminded = lapsed = 0
    with Session(create_db_engine(database_url)) as session:
        due = session.scalars(
            select(Registration)
            .where(Registration.status == "PAYMENT_PENDING", Registration.payment_reminder_sent_at.is_(None),
                   Registration.created_at <= now - REMIND_AFTER)
            .with_for_update(skip_locked=True)
        ).all()
        for registration in due:
            queue_email(session, registration.leader_email, "HackMatrix registration: finish your payment", action_email(
                title="Your registration is waiting for payment",
                intro="You started registering your team for HackMatrix 26 but the payment has not gone through yet.",
                rows=[("Team", registration.team_name), ("Amount due", f"₹{registration.quoted_amount_minor // 100}")],
                steps=["Open the registration page.", "Submit the same team details again to return to the same payment.", "Pay with Razorpay."],
                action="Finish registration", url=f"{payments.app_url()}/register",
                note="We hold your team name and members for one more hour. After that the registration lapses and you will need to register again.",
                eyebrow="Payment pending",
            ))
            registration.payment_reminder_sent_at = now
            reminded += 1
        session.commit()

        # Without a provider client a live payment cannot be ruled out, so nothing lapses.
        if api is None:
            return reminded, lapsed
        expired = session.scalars(
            select(Registration.id).where(Registration.status == "PAYMENT_PENDING",
                                          Registration.payment_reminder_sent_at <= now - LAPSE_AFTER_REMINDER)
        ).all()
        for registration_id in expired:
            registration = session.get(Registration, registration_id, with_for_update={"skip_locked": True})
            if registration is None or registration.status != "PAYMENT_PENDING":
                session.rollback()
                continue
            orders = session.scalars(select(PaymentOrder).where(
                PaymentOrder.registration_id == registration.id, PaymentOrder.status == "CREATED").with_for_update()).all()
            for order in orders:
                payments.reconcile_order(session, api, order, force=True)
            session.flush()
            if registration.status != "PAYMENT_PENDING" or any(payments.provider_has_live_payment(api, order) for order in orders):
                session.commit()
                continue
            registration.status = "ABANDONED"
            for order in orders:
                if order.status == "CREATED":
                    order.status = "CANCELLED"
            session.commit()
            lapsed += 1
    return reminded, lapsed
