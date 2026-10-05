import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.api.deps import authenticated_user, bearer_claims
from src.auth.security import decode_access_token, hash_password, verify_password
from src.communications.templates import action_email
from src.auth.service import AuthenticationError, authenticate, issue_tokens, rotate_tokens
from src.auth.sessions import InvalidRefreshSession
from src.auth.policies import RoleCode
from src.auth.rate_limits import is_blocked, is_limited
from src.communications.outbox import dispatch_pending, queue_email
from src.db.models.identity import AdminInvite, AuditLog, PasswordResetToken, RefreshSession, Role, User, UserStatus
from src.db.models.registration import ActivationToken
from src.db.session import create_db_engine


class LoginRequest(BaseModel):
    email: str
    password: str
    portal: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=256)


class AdminInviteRequest(BaseModel):
    email: str
    role: RoleCode


class AcceptInviteRequest(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=256)


class ActivateRequest(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=256)


def auth_router(private_key: Ed25519PrivateKey, public_key: Ed25519PublicKey, email_sender) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

    def session_for(request: Request):
        with Session(create_db_engine(request.app.state.database_url)) as session:
            yield session

    def tokens(access: str, refresh: str) -> dict[str, dict[str, str]]:
        return {"tokens": {"access_token": access, "refresh_token": refresh}}

    @router.get("/activation")
    def activation_preview(token: str, request: Request):
        with Session(create_db_engine(request.app.state.database_url)) as session:
            row = session.scalar(select(ActivationToken).where(ActivationToken.token_hash == sha256(token.encode()).hexdigest()))
            now = datetime.now(UTC)
            if row is None or row.consumed_at is not None or row.expires_at <= now:
                raise HTTPException(400, "invalid activation token")
            user = session.get(User, row.user_id)
            if user is None:
                raise HTTPException(400, "invalid activation token")
            return {"email": user.email_normalized}

    @router.post("/activate")
    def activate(payload: ActivateRequest, request: Request):
        with Session(create_db_engine(request.app.state.database_url)) as session:
            row = session.scalar(select(ActivationToken).where(ActivationToken.token_hash == sha256(payload.token.encode()).hexdigest()))
            now = datetime.now(UTC)
            if row is None or row.consumed_at is not None or row.expires_at <= now:
                raise HTTPException(400, "invalid activation token")
            user = session.scalar(select(User).options(selectinload(User.roles)).where(User.id == row.user_id))
            if user is None or user.status != UserStatus.PENDING_ACTIVATION:
                raise HTTPException(400, "invalid activation token")
            user.password_hash = hash_password(payload.password)
            user.status = UserStatus.ACTIVE
            row.consumed_at = now
            access, refresh = issue_tokens(session, user, private_key, "hackmatrix-api", "hackmatrix-bff")
            session.commit()
            return {"tokens": {"access_token": access, "refresh_token": refresh.token}, "user": user_payload(user)}

    def dispatch(request: Request, background: BackgroundTasks) -> None:
        background.add_task(dispatch_pending, request.app.state.database_url, email_sender)

    @router.post("/login")
    def login(payload: LoginRequest, request: Request):
        with Session(create_db_engine(request.app.state.database_url)) as session:
            email = payload.email.strip().lower()
            # The BFF forwards the browser's address; it only narrows the tight per-client budget.
            client = (request.headers.get("X-Client-IP") or (request.client.host if request.client else "unknown"))[:64]
            limits = ((f"login:{email}:{client}", 5), (f"login:{email}", 30))
            now = datetime.now(UTC)
            # Checked before the password so a correct guess after lockout is still refused. The
            # per-account cap is high so locking someone else out takes sustained effort.
            if any(is_blocked(session, key, now, limit, timedelta(minutes=15)) for key, limit in limits):
                raise HTTPException(429, "too many login attempts")
            try:
                user = authenticate(session, payload.email, payload.password, payload.portal)
            except AuthenticationError:
                limited = [is_limited(session, key, now, limit, timedelta(minutes=15)) for key, limit in limits]
                session.commit()
                if any(limited):
                    raise HTTPException(429, "too many login attempts")
                raise HTTPException(401, "invalid credentials") from None
            access, refresh = issue_tokens(session, user, private_key, "hackmatrix-api", "hackmatrix-bff")
            session.add(AuditLog(actor_user_id=user.id, action="login", target_type="user", target_id=user.email_normalized, metadata_json={"portal": payload.portal}))
            session.commit()
            return {**tokens(access, refresh.token), "user": user_payload(user)}

    @router.post("/refresh")
    def refresh(payload: RefreshRequest, request: Request):
        with Session(create_db_engine(request.app.state.database_url)) as session:
            try:
                access, refresh = rotate_tokens(session, payload.refresh_token, private_key, "hackmatrix-api", "hackmatrix-bff")
            except (AuthenticationError, InvalidRefreshSession):
                raise HTTPException(401, "invalid refresh token") from None
            session.commit()
            return tokens(access, refresh.token)

    @router.post("/logout", status_code=204)
    def logout(payload: RefreshRequest, request: Request):
        with Session(create_db_engine(request.app.state.database_url)) as session:
            from hashlib import sha256
            row = session.scalar(select(RefreshSession).where(RefreshSession.token_hash == sha256(payload.refresh_token.encode()).hexdigest()))
            if row is not None and row.revoked_at is None:
                user = session.get(User, row.user_id)
                row.revoked_at = datetime.now(UTC)
                session.add(AuditLog(actor_user_id=row.user_id, action="logout", target_type="user", target_id=user.email_normalized if user else "", metadata_json={}))
                session.commit()

    def super_admin(request: Request, authorization: str) -> User:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "invalid access token")
        try:
            claims = decode_access_token(authorization.removeprefix("Bearer "), public_keys={"current": public_key}, issuer="hackmatrix-api", audience="hackmatrix-bff")
        except Exception:
            raise HTTPException(401, "invalid access token") from None
        with Session(create_db_engine(request.app.state.database_url)) as session:
            user = session.scalar(select(User).options(selectinload(User.roles)).where(User.id == claims["sub"]))
            if user is None or RoleCode.SUPER_ADMIN not in {role.code for role in user.roles}:
                raise HTTPException(403, "admin access required")
            session.expunge(user)
            return user

    @router.post("/admin/invites", status_code=201)
    def create_invite(payload: AdminInviteRequest, request: Request, background: BackgroundTasks, authorization: str = Header()):
        admin = super_admin(request, authorization)
        with Session(create_db_engine(request.app.state.database_url)) as session:
            token = token_urlsafe(32)
            invite = AdminInvite(email_normalized=payload.email.strip().lower(), role_code=payload.role, token_hash=sha256(token.encode()).hexdigest(), expires_at=datetime.now(UTC) + timedelta(days=7), invited_by_user_id=admin.id)
            session.add(invite)
            session.flush()
            session.add(AuditLog(actor_user_id=admin.id, action="admin_invite_created", target_type="admin_invite", target_id=str(invite.id), metadata_json={"role": str(payload.role)}))
            admin_url = os.getenv("ADMIN_APP_URL", "http://localhost:3001")
            queue_email(session, invite.email_normalized, "HackMatrix staff invite", action_email(
                title="You are invited to HackMatrix", intro=f"You have been invited as {payload.role.value.replace('_', ' ').title()}.",
                rows=[("Account", invite.email_normalized), ("Role", payload.role.value)],
                steps=["Open the invite link.", "Choose a password.", "Sign in to the admin portal."],
                action="Accept invite", url=f"{admin_url}/invite?token={token}", note="This invite expires in 7 days.",
            ))
            session.commit()
            dispatch(request, background)
            return {"id": str(invite.id), "email": invite.email_normalized, "role": payload.role.value, "expires_at": invite.expires_at.isoformat()}

    @router.post("/admin/invites/accept", status_code=204)
    def accept_invite(payload: AcceptInviteRequest, request: Request):
        with Session(create_db_engine(request.app.state.database_url)) as session:
            invite = session.scalar(select(AdminInvite).where(AdminInvite.token_hash == sha256(payload.token.encode()).hexdigest()))
            now = datetime.now(UTC)
            if invite is None or invite.accepted_at is not None or invite.revoked_at is not None or invite.expires_at <= now:
                raise HTTPException(400, "invalid invite token")
            user = session.scalar(select(User).options(selectinload(User.roles)).where(User.email_normalized == invite.email_normalized))
            role = session.get(Role, invite.role_code)
            if user is None:
                user = User(email_normalized=invite.email_normalized, display_name=invite.email_normalized.split("@", 1)[0], password_hash=hash_password(payload.password), status=UserStatus.ACTIVE, roles=[role])
                session.add(user)
            else:
                user.password_hash = hash_password(payload.password)
                user.status = UserStatus.ACTIVE
                if role not in user.roles:
                    user.roles.append(role)
            invite.accepted_at = now
            session.add(AuditLog(actor_user_id=user.id, action="admin_invite_accepted", target_type="admin_invite", target_id=str(invite.id), metadata_json={}))
            session.commit()

    @router.post("/password/forgot", status_code=202)
    def forgot_password(payload: ForgotPasswordRequest, request: Request, background: BackgroundTasks):
        with Session(create_db_engine(request.app.state.database_url)) as session:
            email = payload.email.strip().lower()
            if is_limited(session, f"reset:{email}", datetime.now(UTC), 3, timedelta(hours=1)):
                session.commit()
                raise HTTPException(429, "too many reset requests")
            user = session.scalar(select(User).where(User.email_normalized == email))
            if user is not None:
                token = token_urlsafe(32)
                app_url = os.getenv('PARTICIPANT_APP_URL', 'http://localhost:3000')
                if user.status == UserStatus.PENDING_ACTIVATION:
                    session.add(ActivationToken(user_id=user.id, token_hash=sha256(token.encode()).hexdigest(), expires_at=datetime.now(UTC) + timedelta(minutes=30)))
                    queue_email(session, email, "Activate your HackMatrix account", action_email(
                        title="Activate your account", intro="Use this link within 30 minutes to create your password.",
                        rows=[("Account", email)], steps=["Open the activation link.", "Create a password."],
                        action="Activate account", url=f"{app_url}/activate?token={token}",
                        note="If you did not ask for this, ignore this email.",
                    ))
                elif user.status == UserStatus.ACTIVE:
                    session.add(PasswordResetToken(user_id=user.id, token_hash=sha256(token.encode()).hexdigest(), expires_at=datetime.now(UTC) + timedelta(hours=1)))
                    queue_email(session, email, "Reset your HackMatrix password", action_email(
                        title="Reset your password", intro="Use this link within 1 hour to choose a new password.",
                        rows=[("Account", email)], steps=["Open the reset link.", "Choose a new password."],
                        action="Reset password", url=f"{app_url}/reset-password?token={token}",
                        note="If you did not ask for this, ignore this email.",
                    ))
                session.add(AuditLog(actor_user_id=user.id, action="password_reset_requested", target_type="user", target_id=email, metadata_json={}))
            session.commit()
        dispatch(request, background)

    @router.post("/password/reset", status_code=204)
    def reset_password(payload: ResetPasswordRequest, request: Request):
        with Session(create_db_engine(request.app.state.database_url)) as session:
            token = session.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == sha256(payload.token.encode()).hexdigest()))
            now = datetime.now(UTC)
            if token is None or token.consumed_at is not None or token.expires_at <= now:
                raise HTTPException(400, "invalid reset token")
            user = session.get(User, token.user_id)
            token.consumed_at = now
            user.password_hash = hash_password(payload.password)
            for refresh_session in session.scalars(select(RefreshSession).where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))):
                refresh_session.revoked_at = now
            session.add(AuditLog(actor_user_id=user.id, action="password_reset", target_type="user", target_id=user.email_normalized, metadata_json={}))
            session.commit()

    @router.get("/me")
    def me(request: Request, authorization: str = Header()):
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "invalid access token")
        try:
            claims = decode_access_token(authorization.removeprefix("Bearer "), public_keys={"current": public_key}, issuer="hackmatrix-api", audience="hackmatrix-bff")
        except Exception:
            raise HTTPException(401, "invalid access token") from None
        with Session(create_db_engine(request.app.state.database_url)) as session:
            user = session.scalar(select(User).options(selectinload(User.roles)).where(User.id == claims["sub"]))
            if user is None or user.status != UserStatus.ACTIVE:
                raise HTTPException(401, "invalid access token")
            return {"user": user_payload(user)}

    @router.post("/password/change", status_code=204)
    def change_password(payload: ChangePasswordRequest, request: Request, authorization: str = Header()):
        session, user = authenticated_user(request, authorization, public_key)
        with session:
            now = datetime.now(UTC)
            key = f"password-change:{user.id}"
            if is_blocked(session, key, now, 5, timedelta(minutes=15)):
                raise HTTPException(429, "too many attempts")
            if user.password_hash is None or not verify_password(payload.current_password, user.password_hash):
                is_limited(session, key, now, 5, timedelta(minutes=15))
                session.commit()
                raise HTTPException(400, "current password is incorrect")
            user.password_hash = hash_password(payload.new_password)
            current_sid = bearer_claims(authorization, public_key).get("sid")
            for refresh_session in session.scalars(select(RefreshSession).where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))):
                if str(refresh_session.id) != current_sid:
                    refresh_session.revoked_at = now
            session.add(AuditLog(actor_user_id=user.id, action="password_changed", target_type="user", target_id=user.email_normalized, metadata_json={}))
            session.commit()

    return router


def user_payload(user: User) -> dict[str, object]:
    return {"id": str(user.id), "email": user.email_normalized, "display_name": user.display_name, "roles": [str(role.code) for role in user.roles]}
