import logging
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.sql import text
from app.config import DATABASE_URL, DB_CONNECT_TIMEOUT

logger = logging.getLogger(__name__)

_engine_connect_args = {"connect_timeout": max(5, DB_CONNECT_TIMEOUT)}

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    connect_args=_engine_connect_args,
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
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
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
    product = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    uploaded_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    uploaded_by = relationship("User", back_populates="documents_uploaded")
    audit_logs = relationship("AuditLog", back_populates="document")

class RFIProject(Base):
    __tablename__ = "rfi_projects"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, index=True)
    slug = Column(String, unique=True, nullable=True, index=True)
    json_data = Column(JSONB, nullable=True)
    status = Column(String, default="generating")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    editing_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    lock_acquired_at = Column(DateTime(timezone=True), nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    editing_user = relationship("User", foreign_keys=[editing_user_id])
    deleted_by = relationship("User", foreign_keys=[deleted_by_user_id])


class RFIPdfProject(Base):
    __tablename__ = "rfi_pdf_projects"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, index=True)
    slug = Column(String, unique=True, nullable=True, index=True)
    source_storage_key = Column(String, nullable=True)
    parsed_markdown = Column(Text, nullable=True)
    editor_markdown = Column(Text, nullable=True)
    editor_html = Column(Text, nullable=True)
    requirements = Column(JSONB, nullable=True, default=list)
    entity_refs = Column(JSONB, nullable=True, default=list)
    metadata_json = Column("metadata_json", JSONB, nullable=True, default=dict)
    status = Column(String, default="uploading", nullable=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    editing_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    lock_acquired_at = Column(DateTime(timezone=True), nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    editing_user = relationship("User", foreign_keys=[editing_user_id])
    deleted_by = relationship("User", foreign_keys=[deleted_by_user_id])


class RFPProject(Base):
    __tablename__ = "rfp_projects"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, nullable=True, index=True)
    product = Column(String, nullable=False, index=True)
    project_name = Column(String, nullable=True)
    project_description = Column(Text, nullable=True)
    content = Column(Text, nullable=True)
    chat_messages = Column(JSONB, nullable=True, default=list)
    status = Column(String, default="draft")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    editing_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    lock_acquired_at = Column(DateTime(timezone=True), nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    editing_user = relationship("User", foreign_keys=[editing_user_id])
    deleted_by = relationship("User", foreign_keys=[deleted_by_user_id])


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(160), nullable=False)
    resource_type = Column(String(120), nullable=False)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    rfi_project_id = Column(Integer, ForeignKey("rfi_projects.id", ondelete="SET NULL"), nullable=True)
    rfi_pdf_project_id = Column(Integer, ForeignKey("rfi_pdf_projects.id", ondelete="SET NULL"), nullable=True)
    rfp_project_id = Column(Integer, ForeignKey("rfp_projects.id", ondelete="SET NULL"), nullable=True)
    details = Column(JSONB)
    ip_address = Column(String(45))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    user = relationship("User", back_populates="audit_logs")
    document = relationship("Document", back_populates="audit_logs")


def init_db():
    logger.info(
        "Initializing database (PostgreSQL connect_timeout=%ss)...",
        max(5, DB_CONNECT_TIMEOUT),
    )
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
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS product VARCHAR"
            ))
            conn.execute(text(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_documents_product ON documents(product)"
            ))
            conn.commit()
            
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS rfi_projects (
                    id SERIAL PRIMARY KEY,
                    filename VARCHAR,
                    slug VARCHAR UNIQUE,
                    json_data JSONB,
                    status VARCHAR DEFAULT 'generating',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_rfi_projects_user_id ON rfi_projects(user_id)"))
            conn.execute(text(
                "ALTER TABLE rfi_projects ADD COLUMN IF NOT EXISTS slug VARCHAR"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_rfi_projects_slug ON rfi_projects(slug) WHERE slug IS NOT NULL"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_projects ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_projects ADD COLUMN IF NOT EXISTS editing_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_projects ADD COLUMN IF NOT EXISTS lock_acquired_at TIMESTAMP WITH TIME ZONE"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_rfi_projects_editing_user_id ON rfi_projects(editing_user_id)"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_projects ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_projects ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_projects ADD COLUMN IF NOT EXISTS deleted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_rfi_projects_is_deleted ON rfi_projects(is_deleted)"
            ))
            conn.commit()

        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS rfi_pdf_projects (
                    id SERIAL PRIMARY KEY,
                    filename VARCHAR,
                    slug VARCHAR UNIQUE,
                    source_storage_key VARCHAR,
                    parsed_markdown TEXT,
                    editor_markdown TEXT,
                    editor_html TEXT,
                    requirements JSONB DEFAULT '[]'::jsonb,
                    entity_refs JSONB DEFAULT '[]'::jsonb,
                    metadata_json JSONB DEFAULT '{}'::jsonb,
                    status VARCHAR NOT NULL DEFAULT 'uploading',
                    error_message TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    editing_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    lock_acquired_at TIMESTAMP WITH TIME ZONE,
                    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    deleted_at TIMESTAMP WITH TIME ZONE,
                    deleted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
                )
            """))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_rfi_pdf_projects_slug ON rfi_pdf_projects(slug) WHERE slug IS NOT NULL"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_rfi_pdf_projects_user_id ON rfi_pdf_projects(user_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_rfi_pdf_projects_editing_user_id ON rfi_pdf_projects(editing_user_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_rfi_pdf_projects_is_deleted ON rfi_pdf_projects(is_deleted)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_rfi_pdf_projects_status ON rfi_pdf_projects(status)"
            ))
            # CREATE TABLE IF NOT EXISTS does not mutate existing tables: older installs may
            # miss columns added later (e.g. entity_refs). Align schema incrementally.
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS filename VARCHAR"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS slug VARCHAR"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS source_storage_key VARCHAR"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS parsed_markdown TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS editor_markdown TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS editor_html TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS "
                "requirements JSONB DEFAULT '[]'::jsonb"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS "
                "entity_refs JSONB DEFAULT '[]'::jsonb"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS "
                "metadata_json JSONB DEFAULT '{}'::jsonb"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS "
                "status VARCHAR NOT NULL DEFAULT 'uploading'"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS error_message TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS "
                "created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS "
                "updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS "
                "user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS "
                "editing_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS "
                "lock_acquired_at TIMESTAMP WITH TIME ZONE"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS "
                "is_deleted BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE"
            ))
            conn.execute(text(
                "ALTER TABLE rfi_pdf_projects ADD COLUMN IF NOT EXISTS "
                "deleted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            ))
            conn.commit()

        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS rfp_projects (
                    id SERIAL PRIMARY KEY,
                    slug VARCHAR UNIQUE,
                    product VARCHAR NOT NULL,
                    project_name VARCHAR,
                    project_description TEXT,
                    content TEXT,
                    chat_messages JSONB DEFAULT '[]'::jsonb,
                    status VARCHAR DEFAULT 'draft',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    editing_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    lock_acquired_at TIMESTAMP WITH TIME ZONE
                )
            """))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS slug VARCHAR"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS product VARCHAR NOT NULL DEFAULT 'Unassigned'"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ALTER COLUMN product DROP DEFAULT"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS project_name VARCHAR"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS project_description TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS content TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS chat_messages JSONB DEFAULT '[]'::jsonb"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'draft'"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS editing_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS lock_acquired_at TIMESTAMP WITH TIME ZONE"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_rfp_projects_slug ON rfp_projects(slug) WHERE slug IS NOT NULL"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_rfp_projects_product ON rfp_projects(product)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_rfp_projects_user_id ON rfp_projects(user_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_rfp_projects_editing_user_id ON rfp_projects(editing_user_id)"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE"
            ))
            conn.execute(text(
                "ALTER TABLE rfp_projects ADD COLUMN IF NOT EXISTS deleted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_rfp_projects_is_deleted ON rfp_projects(is_deleted)"
            ))
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
                    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE"
            ))
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
                    rfi_project_id INTEGER REFERENCES rfi_projects(id) ON DELETE SET NULL,
                    rfp_project_id INTEGER REFERENCES rfp_projects(id) ON DELETE SET NULL,
                    details JSONB,
                    ip_address VARCHAR(45),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """))
            conn.execute(text(
                "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS rfi_project_id INTEGER REFERENCES rfi_projects(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS rfp_project_id INTEGER REFERENCES rfp_projects(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS rfi_pdf_project_id INTEGER REFERENCES rfi_pdf_projects(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_audit_logs_rfi_pdf_project_id ON audit_logs(rfi_pdf_project_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_audit_logs_user_id ON audit_logs(user_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_audit_logs_document_id ON audit_logs(document_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs(created_at)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_audit_logs_action_created_at ON audit_logs(action, created_at)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_audit_logs_rfi_project_id ON audit_logs(rfi_project_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_audit_logs_rfp_project_id ON audit_logs(rfp_project_id)"
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
    except Exception:
        logger.exception("Failed to initialize database — check DATABASE_URL / VPN / DB_CONNECT_TIMEOUT")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
