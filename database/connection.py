from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from google.cloud.sql.connector import Connector
from contextlib import contextmanager
from loguru import logger
import pg8000

from config.settings import settings
from database.models import Base


def get_connection():
    """
    Creates a Cloud SQL connection using the Cloud SQL Python Connector.
    This handles auth automatically when running on GCP.
    """
    connector = Connector()

    def connect():
        conn = connector.connect(
            settings.db_instance,
            "pg8000",
            user=settings.db_user,
            password=settings.db_password,
            db=settings.db_name,
        )
        return conn

    engine = create_engine(
        "postgresql+pg8000://",
        creator=connect,
        pool_size=5,
        max_overflow=2,
        pool_timeout=30,
        pool_recycle=1800,
    )
    return engine


def get_local_connection():
    """
    For local dev/testing without Cloud SQL.
    Set DATABASE_URL env var to a local postgres or sqlite URL.
    """
    import os
    db_url = os.environ.get("DATABASE_URL", "sqlite:///./leasing_auditor_dev.db")
    engine = create_engine(db_url, echo=False)
    return engine


def get_engine():
    """
    Returns the appropriate engine based on environment.
    """
    import os
    if os.environ.get("ENVIRONMENT") == "production":
        logger.info("Using Cloud SQL connection")
        return get_connection()
    else:
        logger.info("Using local database connection")
        return get_local_connection()


engine = get_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_db() -> Session:
    """
    Context manager for database sessions.
    Always use this for DB operations:

        with get_db() as db:
            db.add(some_object)
            db.commit()
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        db.close()


def init_db():
    """
    Creates all tables. Run once during setup or deployment.
    """
    logger.info("Initializing database tables...")
    Base.metadata.create_all(bind=engine)
    logger.success("Database tables created.")


if __name__ == "__main__":
    init_db()
