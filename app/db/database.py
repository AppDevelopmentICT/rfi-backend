import logging
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    ForeignKey,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.sql import text
from app.config import DATABASE_URL

logger = logging.getLogger(__name__)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    pocketbase_id = Column(String(255), unique=True, nullable=False, index=True)
    email = Column(String(510), nullable=False)
    name = Column(String(500))
    avatar_url = Column(String(2048))
    verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    documents_uploaded = relationship("Document", back_populates="uploaded_by")
    audit_logs = relationship("AuditLog", back_populates="user")


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, index=True)
    status = Column(String)
    minio_key = Column(String, nullable=True, index=True)
    minio_etag = Column(String, nullable=True)
    source = Column(String, default="upload")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    uploaded_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    uploaded_by = relationship("User", back_populates="documents_uploaded")
    audit_logs = relationship("AuditLog", back_populates="document")

class RFIProject(Base):
    __tablename__ = "rfi_projects"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, index=True)
    json_data = Column(JSONB, nullable=True)
    status = Column(String, default="generating")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    user = relationship("User")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(160), nullable=False)
    resource_type = Column(String(120), nullable=False)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    details = Column(JSONB)
    ip_address = Column(String(45))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    user = relationship("User", back_populates="audit_logs")
    document = relationship("Document", back_populates="audit_logs")


def init_db():
    logger.info("Initializing Database...")
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()

        Base.metadata.create_all(bind=engine)

        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS minio_key VARCHAR"
            ))
            conn.execute(text(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'upload'"
            ))
            conn.execute(text(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS minio_etag VARCHAR"
            ))
            conn.execute(text(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()"
            ))
            conn.commit()
            
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS rfi_projects (
                    id SERIAL PRIMARY KEY,
                    filename VARCHAR,
                    json_data JSONB,
                    status VARCHAR DEFAULT 'generating',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_rfi_projects_user_id ON rfi_projects(user_id)"))
            conn.commit()

        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    pocketbase_id VARCHAR(255) NOT NULL UNIQUE,
                    email VARCHAR(510) NOT NULL,
                    name VARCHAR(500),
                    avatar_url VARCHAR(2048),
                    verified BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_users_pocketbase_id ON users(pocketbase_id)"
            ))
            conn.commit()

        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    action VARCHAR(160) NOT NULL,
                    resource_type VARCHAR(120) NOT NULL,
                    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                    details JSONB,
                    ip_address VARCHAR(45),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_audit_logs_user_id ON audit_logs(user_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_audit_logs_document_id ON audit_logs(document_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs(created_at)"
            ))
            conn.commit()

        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS uploaded_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_documents_uploaded_by_user_id ON documents(uploaded_by_user_id)"
            ))
            conn.commit()

        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
