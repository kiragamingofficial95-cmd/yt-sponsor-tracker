from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

url = settings.DATABASE_URL
# Railway gives postgresql:// — map to SQLAlchemy psycopg v3 dialect
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql+psycopg://", 1)
elif url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

def ensure_schema():
    """Create tables + add columns for deploys made before a model change."""
    from . import models  # noqa: ensure mapped
    Base.metadata.create_all(bind=engine)
    insp = inspect(engine)
    if "creators" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("creators")}
        stmts = []
        if "history_done" not in cols:
            stmts.append("ALTER TABLE creators ADD COLUMN history_done BOOLEAN DEFAULT FALSE")
        if "total_checked" not in cols:
            stmts.append("ALTER TABLE creators ADD COLUMN total_checked INTEGER DEFAULT 0")
    if "videos" in insp.get_table_names():
        vcols = {c["name"] for c in insp.get_columns("videos")}
        for col in ("desc_len", "tx_len", "hint_score", "pipe_ver"):
            if col not in vcols:
                stmts.append(f"ALTER TABLE videos ADD COLUMN {col} INTEGER DEFAULT 0")
        if stmts:
            with engine.begin() as conn:
                for s in stmts:
                    conn.execute(text(s))

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
