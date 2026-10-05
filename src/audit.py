from sqlalchemy.orm import Session

from src.db.models.identity import AuditLog, User


def audit(session: Session, actor: User | None, action: str, target_type: str, target_id: object, **metadata: object) -> None:
    session.add(AuditLog(
        actor_user_id=actor.id if actor else None,
        action=action,
        target_type=target_type,
        target_id=str(target_id)[:100],
        metadata_json={key: value for key, value in metadata.items() if value is not None},
    ))
