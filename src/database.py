"""
Database connection and migration management.
"""
import os
import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from models import Base

logger = logging.getLogger(__name__)

# Database configuration
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///lakbay_assignments.db')

# Create engine with appropriate settings
if DATABASE_URL.startswith('sqlite'):
    # SQLite settings for development
    engine = create_engine(
        DATABASE_URL,
        poolclass=StaticPool,
        connect_args={
            'check_same_thread': False,
            'timeout': 20
        },
        echo=False  # Set to True for SQL debugging
    )
else:
    # PostgreSQL settings for production
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=300,
        echo=False
    )

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Get database session with automatic cleanup"""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_database():
    """Initialize database tables"""
    try:
        logger.info("Initializing database...")
        
        # Create all tables
        Base.metadata.create_all(bind=engine)
        
        logger.info("Database initialized successfully")
        return True
        
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return False


def check_database_connection() -> bool:
    """Check if database connection is working"""
    try:
        with get_db_session() as db:
            # Simple query to test connection
            if DATABASE_URL.startswith('sqlite'):
                result = db.execute(text("SELECT 1")).fetchone()
            else:
                result = db.execute(text("SELECT version()")).fetchone()
            
            logger.info("Database connection successful")
            return True
            
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        return False


def migrate_database():
    """Run database migrations"""
    try:
        logger.info("Running database migrations...")
        
        with get_db_session() as db:
            # Check if this is a fresh database
            try:
                # Try to query a table to see if schema exists
                db.execute(text("SELECT COUNT(*) FROM users LIMIT 1")).fetchone()
                logger.info("Existing database detected, checking for migrations...")
                
            except Exception:
                logger.info("Fresh database detected, creating schema...")
                
            # Create/update all tables (idempotent)
            Base.metadata.create_all(bind=engine)
            
            logger.info("Database migrations completed successfully")
            return True
            
    except Exception as e:
        logger.error(f"Database migration failed: {e}")
        return False


def reset_database():
    """Drop and recreate all tables (use with caution!)"""
    try:
        logger.warning("Resetting database - ALL DATA WILL BE LOST!")
        
        # Drop all tables
        Base.metadata.drop_all(bind=engine)
        
        # Recreate all tables
        Base.metadata.create_all(bind=engine)
        
        logger.info("Database reset completed")
        return True
        
    except Exception as e:
        logger.error(f"Database reset failed: {e}")
        return False


def get_db_stats() -> dict:
    """Get basic database statistics"""
    try:
        with get_db_session() as db:
            stats = {}
            
            # Count records in main tables
            tables = ['users', 'shifts', 'task_templates', 'assignments', 'approval_requests', 'audit_logs']
            
            for table in tables:
                try:
                    result = db.execute(text(f"SELECT COUNT(*) FROM {table}")).fetchone()
                    stats[table] = result[0] if result else 0
                except Exception as e:
                    logger.warning(f"Failed to count {table}: {e}")
                    stats[table] = "error"
            
            return stats
            
    except Exception as e:
        logger.error(f"Failed to get database stats: {e}")
        return {}
