"""
Smart City Pollution Monitoring System
Backend – Database setup (SQLAlchemy + SQLite)

For AWS deployment this module swaps SQLite for DynamoDB/RDS by
changing DATABASE_URL via the environment variable.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# SQLite locally; override with DATABASE_URL env var for production
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./pollution.db",
)

engine = create_engine(
    DATABASE_URL,
    # SQLite needs connect_args for multi-threaded Flask use
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency-style session factory (used by Flask routes)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables (called at application startup)."""
    # Import models so SQLAlchemy registers them before create_all
    import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
