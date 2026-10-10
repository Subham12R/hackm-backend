"""Who an email comes from.

Three mailboxes on the sending domain, each verified once in Resend (verifying the domain covers all):

- noreply@: account, payment and security mail; replies go to support@.
- support@: replies to the contact form; people answer straight back to it.
- announcements@: announcements and staff-written emails; replies go to support@.

Every address can be overridden (EMAIL_FROM_NOREPLY, EMAIL_FROM_SUPPORT, EMAIL_FROM_ANNOUNCEMENTS) with a
full `Name <address>` value; EMAIL_FROM still works as the old name for the noreply sender.
"""

import os
from email.utils import parseaddr
from typing import Literal

Sender = Literal["noreply", "support", "announcements"]
SENDERS: tuple[Sender, ...] = ("noreply", "support", "announcements")
DISPLAY_NAMES: dict[str, str] = {"noreply": "HackMatrix", "support": "HackMatrix Support", "announcements": "HackMatrix Announcements"}


def email_domain() -> str:
    return (os.getenv("EMAIL_DOMAIN") or "hackmatrix.codes").strip().lower()


def from_address(sender: str) -> str:
    """`Name <mailbox@domain>` for the From header."""
    key = sender if sender in SENDERS else "noreply"
    configured = os.getenv(f"EMAIL_FROM_{key.upper()}") or (os.getenv("EMAIL_FROM") if key == "noreply" else None)
    return configured or f"{DISPLAY_NAMES[key]} <{key}@{email_domain()}>"


def mailbox(sender: str) -> str:
    """The bare address, for showing staff where an email comes from."""
    return parseaddr(from_address(sender))[1]


def reply_to(sender: str) -> str | None:
    """Replies to automated and announcement mail land in the support inbox, which a person reads."""
    return None if sender == "support" else mailbox("support")


def senders_payload() -> list[dict[str, str]]:
    return [{"key": key, "address": mailbox(key), "from": from_address(key)} for key in SENDERS]
