"""Finals decisions, MLH style: organisers decide quietly, release decisions by email in batches,
and accepted teams confirm attendance (RSVP) before a deadline."""

from datetime import datetime
from hashlib import sha256
from html import escape
from secrets import token_urlsafe
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.communications.outbox import queue_email
from src.communications.templates import ACCENT_SOFT, BODY, INK, MUTED, PANEL, SANS, SERIF, _banner, button, email_shell
from src.db.models.registration import Event, MemberCheckin, Registration, RegistrationMember
from src.payments.service import app_url

RSVP_ANSWERS = ("CONFIRMED", "DECLINED")


def rsvp_digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def released_status(registration: Registration) -> str:
    """What the team has been told: PENDING until a decision is released by email."""
    return registration.finals_notified_status or "PENDING"


def unreleased(session: Session, event_id) -> list[Registration]:
    return list(session.scalars(
        select(Registration).where(
            Registration.event_id == event_id, Registration.status == "PAID",
            Registration.finals_status.in_(("ACCEPTED", "DECLINED")),
            Registration.finals_notified_status.is_distinct_from(Registration.finals_status),
        ).order_by(Registration.team_name).with_for_update()
    ))


def release(session: Session, event: Event, now: datetime) -> dict[str, int]:
    """Email every decided team whose decision changed since it was last told; returns counts."""
    counts = {"ACCEPTED": 0, "DECLINED": 0, "emails": 0}
    for registration in unreleased(session, event.id):
        members = list(session.scalars(
            select(RegistrationMember).where(RegistrationMember.registration_id == registration.id)
            .order_by(RegistrationMember.role.desc(), RegistrationMember.full_name)
        ))
        token = None
        if registration.finals_status == "ACCEPTED":
            token = token_urlsafe(32)
            registration.rsvp_token_hash = rsvp_digest(token)
            registration.rsvp_status, registration.rsvp_at = "PENDING", None
        else:
            registration.rsvp_token_hash = None
        for member in members:
            subject, html = decision_email(registration, member, event, token if member.role == "leader" else None)
            queue_email(session, member.email_normalized, subject, html)
            counts["emails"] += 1
        registration.finals_notified_status = registration.finals_status
        registration.finals_notified_at = now
        counts[registration.finals_status] += 1
    return counts


def apply_rsvp(session: Session, registration: Registration, event: Event, answer: str, now: datetime) -> None:
    if registration.status != "PAID" or registration.finals_status != "ACCEPTED" or released_status(registration) != "ACCEPTED":
        raise HTTPException(409, "this team has no finals place to confirm")
    if event.finals_rsvp_closes_at is not None and now >= event.finals_rsvp_closes_at:
        raise HTTPException(410, "the RSVP deadline has passed; contact the organisers")
    if answer == "DECLINED" and session.scalar(select(MemberCheckin.member_id).where(MemberCheckin.registration_id == registration.id).limit(1)):
        raise HTTPException(409, "this team has already checked in")
    registration.rsvp_status = answer
    registration.rsvp_at = now


def rsvp_payload(registration: Registration, event: Event, now: datetime) -> dict:
    closes = event.finals_rsvp_closes_at
    return {
        "team_name": registration.team_name,
        "decision": released_status(registration),
        "rsvp_status": registration.rsvp_status,
        "rsvp_at": registration.rsvp_at.isoformat() if registration.rsvp_at else None,
        "closes_at": closes.isoformat() if closes else None,
        "open": released_status(registration) == "ACCEPTED" and (closes is None or now < closes),
    }


def decision_email(registration: Registration, member: RegistrationMember, event: Event, rsvp_token: str | None) -> tuple[str, str]:
    first = escape(member.full_name.split(" ")[0])
    team = escape(registration.team_name)
    dashboard = f"{app_url()}/dashboard"
    paragraph = f"margin:16px 0 0;font-family:{SANS};font-size:16px;line-height:1.7;color:{BODY};"
    if registration.finals_status == "ACCEPTED":
        closes = event.finals_rsvp_closes_at
        deadline = f"{closes.astimezone(ZoneInfo(event.timezone)):%d %b %Y, %I:%M %p %Z}" if closes else "as soon as you can"
        title = "You're in the HackMatrix finals!"
        body = f"""<p style="{paragraph}">Hi {first},</p>
            <p style="{paragraph}">Congratulations: <strong style="color:{INK};">{team}</strong> has been selected for the offline finals of HackMatrix 26 at Adamas University.</p>
            <p style="margin:20px 0 0;padding:14px 18px;border-radius:14px;background:{ACCENT_SOFT};font-family:{SANS};font-size:15px;line-height:1.6;color:{INK};">
              <strong>Confirm your spot by {escape(deadline)}.</strong> Places we don't hear back about may go to teams on the waitlist.</p>"""
        if rsvp_token:
            rsvp = f"{app_url()}/rsvp?token={rsvp_token}"
            body += button("Confirm attendance", f"{rsvp}&answer=CONFIRMED")
            body += f"""<p style="margin:14px 0 0;font-family:{SANS};font-size:14px;color:{MUTED};">Can't make it? <a href="{escape(rsvp, quote=True)}&amp;answer=DECLINED" style="color:{BODY};">Let us know</a> so another team can take the spot.</p>"""
        else:
            body += f"""<p style="{paragraph}">Your team leader confirms attendance for the whole team, from their email or the dashboard.</p>"""
            body += button("Open dashboard", dashboard)
    else:
        title = "Your HackMatrix finals application"
        body = f"""<p style="{paragraph}">Hi {first},</p>
            <p style="{paragraph}">Thank you for taking part in HackMatrix 26 with <strong style="color:{INK};">{team}</strong>. We had far more strong teams than places in the offline finals, and this time we weren't able to offer your team a spot.</p>
            <p style="{paragraph}">This isn't a judgement on what you built. Your submission stays on record, and we'd love to see you at the next HackMatrix.</p>"""
        body += button("Open dashboard", dashboard)
    content = f"""<span style="display:inline-block;padding:6px 14px;border-radius:999px;background:{PANEL};font-family:{SANS};font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:{MUTED};">Finals decision</span>
                      <h1 class="headline" style="margin:18px 0 0;font-family:{SERIF};font-size:34px;font-weight:400;line-height:1.18;color:{INK};">{escape(title)}</h1>
                      {body}"""
    preheader = f"{registration.team_name}: finals decision"
    return f"HackMatrix: {title}", email_shell(title=title, preheader=preheader, banner=_banner(), content=content)
