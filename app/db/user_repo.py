from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from app.db.database import SessionLocal, User


def upsert_user_from_pb_record(record: dict) -> User:
    pb_id = str(record["id"])
    email = (record.get("email") or "").strip()
    name = record.get("name") or ""
    avatar = record.get("avatar") or record.get("avatarUrl")
    avatar_url = avatar if isinstance(avatar, str) else None
    verified = bool(record.get("verified", False))

    db: OrmSession = SessionLocal()
    try:
        u = db.query(User).filter(User.pocketbase_id == pb_id).first()
        if u is None:
            u = User(
                pocketbase_id=pb_id,
                email=email or "unknown@local.invalid",
                name=name or None,
                avatar_url=avatar_url,
                verified=verified,
            )
            db.add(u)
            try:
                db.commit()
                db.refresh(u)
            except IntegrityError:
                db.rollback()
                u = db.query(User).filter(User.pocketbase_id == pb_id).first()
                if not u:
                    raise
        else:
            u.email = email or u.email
            u.name = name or u.name
            if avatar_url is not None:
                u.avatar_url = avatar_url
            u.verified = verified
            db.commit()
            db.refresh(u)
        return u
    finally:
        db.close()
