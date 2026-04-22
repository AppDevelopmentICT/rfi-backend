import logging
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
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

class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, index=True)
    status = Column(String)
    minio_key = Column(String, nullable=True, index=True)
    minio_etag = Column(String, nullable=True)
    source = Column(String, default="upload")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

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
        
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
