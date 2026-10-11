"""Certificates and badges: who can get them, issuing drafts, drawing files, publishing and payloads."""

import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.communications.outbox import queue_email
from src.communications.templates import action_email
from src.credentials import render as drawing
from src.credentials.codes import new_code
from src.db.models.credential import BadgeDefinition, CertificateType, Credential
from src.db.models.identity import User
from src.db.models.registration import Event, MemberCheckin, Registration, RegistrationMember, Submission
from src.db.models.stage import Stage, SubmissionAward, Track
from src.db.session import create_db_engine
from src.events import stages as stage_rules
from src.operations import team_code
from src.storage.s3 import CREDENTIAL_FILE_PREFIX, Storage

logger = logging.getLogger(__name__)

CATEGORIES = ("PARTICIPATION", "WINNER", "OTHER")


def issuer() -> str:
    return os.getenv("CREDENTIAL_ISSUER", "ACM Student Chapter, Adamas University")


def site_url() -> str:
    return os.getenv("PARTICIPANT_APP_URL", "https://hackmatrix.codes").rstrip("/")


def verify_url(code: str) -> str:
    return f"{site_url()}/verify/{code}"


@dataclass(frozen=True)
class Filters:
    submitted: bool = False
    finalists: bool = False
    checked_in: bool = False
    awarded: bool = False
    registration_ids: frozenset = frozenset()
    member_ids: frozenset = frozenset()
    emails: frozenset = frozenset()
    query: str = ""


def _awards_by_registration(session: Session, stage: Stage | None) -> dict:
    """registration id -> [(label, track title)], overall placings first."""
    if stage is None:
        return {}
    rows = session.execute(
        select(Submission.registration_id, SubmissionAward.label, SubmissionAward.placement, Track.title)
        .join(SubmissionAward, SubmissionAward.submission_id == Submission.id)
        .outerjoin(Track, Track.id == SubmissionAward.track_id)
        .where(SubmissionAward.stage_id == stage.id)
        .order_by(SubmissionAward.track_id.is_not(None), SubmissionAward.placement)
    ).all()
    awards: dict = defaultdict(list)
    for registration_id, label, _placement, track in rows:
        awards[registration_id].append((label, track or ""))
    return awards


def candidates(session: Session, event: Event, stage: Stage | None, filters: Filters, existing_for: CertificateType | BadgeDefinition | None) -> list[dict]:
    """Members who could receive a credential for `stage`, narrowed by `filters`; every filter must hold."""
    registrations = [
        registration for registration in session.scalars(
            select(Registration).where(Registration.event_id == event.id, Registration.status == "PAID").order_by(Registration.team_name)
        )
        if stage is None or stage_rules.is_eligible(stage, registration)
    ]
    if filters.registration_ids:
        registrations = [registration for registration in registrations if registration.id in filters.registration_ids]
    if filters.finalists:
        registrations = [registration for registration in registrations if registration.finals_status == "ACCEPTED"]
    ids = [registration.id for registration in registrations]
    if not ids:
        return []
    submitted = set()
    if stage is not None:
        submitted = set(session.scalars(select(Submission.registration_id).where(
            Submission.stage_id == stage.id, Submission.status == "SUBMITTED", Submission.registration_id.in_(ids))))
    awards = _awards_by_registration(session, stage)
    checked_in = set(session.scalars(select(MemberCheckin.member_id).where(MemberCheckin.registration_id.in_(ids))))
    issued: dict = {}
    if existing_for is not None:
        column = Credential.certificate_type_id if isinstance(existing_for, CertificateType) else Credential.badge_definition_id
        issued = {credential.member_id: credential for credential in session.scalars(select(Credential).where(column == existing_for.id))}
    members_by_team: dict = defaultdict(list)
    for member in session.scalars(select(RegistrationMember).where(RegistrationMember.registration_id.in_(ids))
                                  .order_by(RegistrationMember.role.desc(), RegistrationMember.full_name)):
        members_by_team[member.registration_id].append(member)
    query = filters.query.strip().lower()
    rows = []
    for registration in registrations:
        if filters.submitted and registration.id not in submitted:
            continue
        if filters.awarded and not awards.get(registration.id):
            continue
        for member in members_by_team[registration.id]:
            if filters.checked_in and member.id not in checked_in:
                continue
            if filters.member_ids and member.id not in filters.member_ids:
                continue
            if filters.emails and member.email_normalized not in filters.emails:
                continue
            if query and query not in f"{member.full_name} {member.email_normalized} {registration.team_name} {team_code(registration.id)}".lower():
                continue
            team_awards = awards.get(registration.id, [])
            existing = issued.get(member.id)
            rows.append({
                "member_id": str(member.id),
                "name": member.full_name,
                "email": member.email_normalized,
                "role": member.role,
                "registration_id": str(registration.id),
                "team_name": registration.team_name,
                "team_id": team_code(registration.id),
                "finalist": registration.finals_status == "ACCEPTED",
                "submitted": registration.id in submitted,
                "checked_in": member.id in checked_in,
                "awards": [{"label": label, "track": track} for label, track in team_awards],
                "placement": team_awards[0][0] if team_awards else "",
                "track": team_awards[0][1] if team_awards else "",
                "issued": {"id": str(existing.id), "code": existing.code, "status": existing.status} if existing else None,
            })
    return rows


@dataclass(frozen=True)
class Recipient:
    member_id: object
    placement: str | None = None
    track: str | None = None
    image_key: str | None = None


def issue(session: Session, actor: User, event: Event, target: CertificateType | BadgeDefinition, recipients: list[Recipient]) -> tuple[list[Credential], int]:
    """Create draft credentials; members who already hold this one are skipped. Caller commits."""
    is_certificate = isinstance(target, CertificateType)
    stage = session.get(Stage, target.stage_id) if target.stage_id else None
    column = Credential.certificate_type_id if is_certificate else Credential.badge_definition_id
    held = set(session.scalars(select(Credential.member_id).where(column == target.id)))
    awards = _awards_by_registration(session, stage)
    created: list[Credential] = []
    skipped = 0
    for recipient in recipients:
        member = session.get(RegistrationMember, recipient.member_id)
        if member is None or member.id in held:
            skipped += 1
            continue
        registration = session.get(Registration, member.registration_id)
        if registration is None or registration.event_id != event.id or registration.status != "PAID":
            skipped += 1
            continue
        default_label, default_track = (awards.get(registration.id) or [("", "")])[0]
        wants_award = not is_certificate or target.category == "WINNER"
        credential = Credential(
            code=new_code(session),
            kind="CERTIFICATE" if is_certificate else "BADGE",
            certificate_type_id=target.id if is_certificate else None,
            badge_definition_id=None if is_certificate else target.id,
            event_id=event.id,
            stage_id=stage.id if stage else None,
            registration_id=registration.id,
            member_id=member.id,
            recipient_name=member.full_name,
            recipient_email=member.email_normalized,
            team_name=registration.team_name,
            event_name=event.name,
            stage_name=stage.name if stage else "",
            placement=recipient.placement if recipient.placement is not None else (default_label if wants_award else ""),
            track=recipient.track if recipient.track is not None else (default_track if wants_award else ""),
            image_key=recipient.image_key or "",
            status="DRAFT",
            version=1,
            issued_by_user_id=actor.id,
            issued_at=datetime.now(UTC),
        )
        if is_certificate:
            credential.render_status = "PENDING"
        else:
            credential.render_status = "DONE"
            credential.png_key = credential.image_key or target.image_key
        session.add(credential)
        held.add(member.id)
        created.append(credential)
    session.flush()
    return created, skipped


def values_for(credential: Credential, certificate_type: CertificateType) -> drawing.Values:
    return drawing.Values(
        recipient_name=credential.recipient_name,
        team_name=credential.team_name,
        event_name=credential.event_name,
        stage_name=credential.stage_name,
        placement=credential.placement,
        track=credential.track,
        issue_date=certificate_type.issue_date or (credential.issued_at or datetime.now(UTC)).date(),
        credential_id=credential.code,
        verify_url=verify_url(credential.code),
    )


def sample_values(certificate_type: CertificateType, stage: Stage, event: Event) -> drawing.Values:
    return drawing.Values(
        recipient_name="Aarav Sharma",
        team_name="Team Byte Bandits",
        event_name=event.name,
        stage_name=stage.name,
        placement="Winner" if certificate_type.category == "WINNER" else "",
        track="Health" if certificate_type.category == "WINNER" else "",
        issue_date=certificate_type.issue_date or date.today(),
        credential_id="HMX-SAMP-LE00",
        verify_url=verify_url("HMX-SAMP-LE00"),
    )


def render_pending(database_url: str, storage: Storage | None, limit: int = 200) -> int:
    """Draw certificates waiting for files, one commit each; a bad template fails only its own rows."""
    if storage is None:
        return 0
    drawn = 0
    artwork_cache: dict = {}
    with Session(create_db_engine(database_url)) as session:
        for _ in range(limit):
            credential = session.scalar(
                select(Credential).where(Credential.kind == "CERTIFICATE", Credential.render_status == "PENDING")
                .order_by(Credential.issued_at).limit(1).with_for_update(skip_locked=True)
            )
            if credential is None:
                break
            certificate_type = session.get(CertificateType, credential.certificate_type_id)
            try:
                artwork = artwork_cache.get(certificate_type.background_key)
                if artwork is None:
                    data = storage.get(certificate_type.background_key)
                    if data is None:
                        raise ValueError("the certificate artwork is missing from storage")
                    artwork = artwork_cache[certificate_type.background_key] = drawing.open_artwork(data)
                files = drawing.render(artwork, certificate_type.layout or [], values_for(credential, certificate_type))
                base = f"{CREDENTIAL_FILE_PREFIX}{credential.code}/v{credential.version}"
                storage.put(f"{base}.png", files.png, "image/png")
                storage.put(f"{base}.pdf", files.pdf, "application/pdf")
                previous = [key for key in (credential.png_key, credential.pdf_key) if key and not key.startswith(base)]
                credential.png_key, credential.pdf_key = f"{base}.png", f"{base}.pdf"
                credential.render_status, credential.render_error = "DONE", ""
                session.commit()
                drawn += 1
                for key in previous:
                    try:
                        storage.delete(key)
                    except Exception:
                        logger.warning("could not delete old credential file %s", key, exc_info=True)
            except Exception as error:
                logger.warning("could not draw credential %s", credential.code, exc_info=True)
                credential.render_status, credential.render_error = "FAILED", str(error)[:500]
                session.commit()
    return drawn


def queue_render(credential: Credential) -> None:
    """Mark a certificate for a fresh drawing (after a reissue or a template change)."""
    if credential.kind == "CERTIFICATE":
        credential.version += 1
        credential.render_status, credential.render_error = "PENDING", ""


def title_of(credential: Credential, certificate_type: CertificateType | None, badge: BadgeDefinition | None) -> str:
    return certificate_type.name if certificate_type else badge.name if badge else "Credential"


def queue_published_emails(session: Session, published: list[Credential], titles: dict) -> int:
    """One email per person listing what was just published for them."""
    by_person: dict = defaultdict(list)
    for credential in published:
        by_person[credential.recipient_email].append(credential)
    for email, items in by_person.items():
        first = items[0]
        names = ", ".join(titles[credential.id] for credential in items)
        rows = [(titles[credential.id], credential.code) for credential in items]
        queue_email(
            session, email,
            f"Your HackMatrix {'credential is' if len(items) == 1 else 'credentials are'} ready",
            action_email(
                title="Your credentials are ready" if len(items) > 1 else f"Your {titles[first.id]} is ready",
                intro=f"Hi {first.recipient_name}, the organisers of {first.event_name} have issued you: {names}. "
                      "Each one has its own public page you can download from, share, and add to LinkedIn.",
                rows=rows,
                steps=[],
                action="View and download",
                url=verify_url(first.code),
                note="Anyone with a credential's link or ID can check that it is genuine. "
                     + ("Your other credentials have their own links on your team leader's dashboard. " if len(items) > 1 else "")
                     + "Spot a typo in your name? Reply to this email and we will fix it.",
                eyebrow="Credential issued",
            ),
            sender="announcements",
        )
    return len(by_person)


def file_urls(credential: Credential, storage: Storage | None) -> dict:
    if storage is None or credential.render_status != "DONE":
        return {"image_url": None, "pdf_url": None}
    return {
        "image_url": storage.public_url(credential.png_key) if credential.png_key else None,
        "pdf_url": storage.public_url(credential.pdf_key) if credential.pdf_key else None,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def public_payload(credential: Credential, title: str, badge: BadgeDefinition | None, storage: Storage | None) -> dict:
    """What anyone with the code may see: never an email, phone or college."""
    return {
        "code": credential.code,
        "kind": credential.kind,
        "title": title,
        "description": badge.description if badge else "",
        "criteria": badge.criteria if badge else "",
        "recipient_name": credential.recipient_name,
        "team_name": credential.team_name,
        "event_name": credential.event_name,
        "stage_name": credential.stage_name,
        "placement": credential.placement,
        "track": credential.track,
        "issuer": issuer(),
        "issued_at": _iso(credential.published_at or credential.issued_at),
        "status": credential.status,
        "revoked_at": _iso(credential.revoked_at),
        "verify_url": verify_url(credential.code),
        **file_urls(credential, storage),
    }


def admin_payload(credential: Credential, title: str, storage: Storage | None, names: dict) -> dict:
    return {
        "id": str(credential.id),
        "code": credential.code,
        "kind": credential.kind,
        "title": title,
        "certificate_type_id": str(credential.certificate_type_id) if credential.certificate_type_id else None,
        "badge_definition_id": str(credential.badge_definition_id) if credential.badge_definition_id else None,
        "stage_id": str(credential.stage_id) if credential.stage_id else None,
        "member_id": str(credential.member_id) if credential.member_id else None,
        "registration_id": str(credential.registration_id),
        "team_id": team_code(credential.registration_id),
        "recipient_name": credential.recipient_name,
        "recipient_email": credential.recipient_email,
        "team_name": credential.team_name,
        "placement": credential.placement,
        "track": credential.track,
        "has_own_image": bool(credential.image_key),
        "status": credential.status,
        "render_status": credential.render_status,
        "render_error": credential.render_error or None,
        "version": credential.version,
        "issued_at": _iso(credential.issued_at),
        "issued_by": names.get(credential.issued_by_user_id),
        "published_at": _iso(credential.published_at),
        "revoked_at": _iso(credential.revoked_at),
        "revoked_reason": credential.revoked_reason or None,
        "verify_url": verify_url(credential.code),
        **file_urls(credential, storage),
    }
