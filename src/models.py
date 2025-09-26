"""
SQLAlchemy models for the task assignment system.
All timestamps are stored in UTC.
"""
import enum
from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, Text, JSON, 
    ForeignKey, Enum, Index, UniqueConstraint
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, Session
from sqlalchemy.sql import func

Base = declarative_base()


class AssignmentStatus(enum.Enum):
    PENDING_ACK = "pending_ack"
    ACTIVE = "active"
    COMPLETED = "completed"
    ENDED_EARLY = "ended_early"
    COVERING = "covering"
    PAUSED_BREAK = "paused_break"
    PAUSED_LUNCH = "paused_lunch"


class ApprovalType(enum.Enum):
    EDIT = "edit"
    END_EARLY = "end_early"
    BREAK15 = "break15"
    LUNCH60 = "lunch60"


class ApprovalStatus(enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    QUEUED_FOR_CAPACITY = "queued_for_capacity"


class User(Base):
    """Discord users with role tracking"""
    __tablename__ = "users"
    
    id = Column(String, primary_key=True)  # Discord user ID as string
    display_name = Column(String, nullable=False)
    is_operator = Column(Boolean, default=False, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    last_comms_lead_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    shifts = relationship("Shift", back_populates="user", cascade="all, delete-orphan")
    assignments = relationship("Assignment", back_populates="user", cascade="all, delete-orphan")
    approval_requests = relationship("ApprovalRequest", back_populates="user", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<User(id={self.id}, display_name={self.display_name})>"


class Shift(Base):
    """User shift tracking with timezone info"""
    __tablename__ = "shifts"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    start_at = Column(DateTime(timezone=True), nullable=False)  # Shift start in UTC
    end_at = Column(DateTime(timezone=True), nullable=True)     # Shift end in UTC (null = active)
    tz_base = Column(String, default="America/Los_Angeles", nullable=False)  # Base timezone for display
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="shifts")
    assignments = relationship("Assignment", back_populates="shift", cascade="all, delete-orphan")
    
    # Indexes
    __table_args__ = (
        Index("idx_shift_user_active", "user_id", "end_at"),
        Index("idx_shift_timerange", "start_at", "end_at"),
    )
    
    def __repr__(self):
        return f"<Shift(id={self.id}, user_id={self.user_id}, start_at={self.start_at})>"


class TaskTemplate(Base):
    """Reusable task definitions for the task pool"""
    __tablename__ = "task_templates"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    priority = Column(Integer, nullable=False, default=100)  # Lower = higher priority
    window_start = Column(DateTime(timezone=True), nullable=True)  # UTC
    window_end = Column(DateTime(timezone=True), nullable=True)    # UTC
    instructions = Column(Text, nullable=True)
    params_schema = Column(JSON, nullable=True)  # JSON Schema for validation
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    assignments = relationship("Assignment", back_populates="template")
    
    # Indexes
    __table_args__ = (
        Index("idx_task_template_active_priority", "is_active", "priority"),
        Index("idx_task_template_window", "window_start", "window_end"),
    )
    
    def __repr__(self):
        return f"<TaskTemplate(id={self.id}, name={self.name}, priority={self.priority})>"


class Assignment(Base):
    """Individual task assignments to users"""
    __tablename__ = "assignments"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    shift_id = Column(Integer, ForeignKey("shifts.id"), nullable=False)
    template_id = Column(Integer, ForeignKey("task_templates.id"), nullable=True)  # Null for ad-hoc tasks
    task_name = Column(String, nullable=False)  # Task name (from template or custom)
    params = Column(JSON, nullable=True, default=dict)  # Task-specific parameters
    status = Column(Enum(AssignmentStatus), nullable=False, default=AssignmentStatus.PENDING_ACK)
    hour_index = Column(Integer, nullable=False)  # 1-9 within the shift
    started_at = Column(DateTime(timezone=True), nullable=True)  # When task was started
    ends_at = Column(DateTime(timezone=True), nullable=True)     # Expected end time (hour boundary)
    ended_at = Column(DateTime(timezone=True), nullable=True)    # Actual end time
    covering_for_user_id = Column(String, ForeignKey("users.id"), nullable=True)  # If covering for someone on break
    forced = Column(Boolean, default=False, nullable=False)      # True if force-assigned
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="assignments", foreign_keys=[user_id])
    covering_for_user = relationship("User", foreign_keys=[covering_for_user_id])
    shift = relationship("Shift", back_populates="assignments")
    template = relationship("TaskTemplate", back_populates="assignments")
    approval_requests = relationship("ApprovalRequest", back_populates="assignment", cascade="all, delete-orphan")
    
    # Indexes
    __table_args__ = (
        Index("idx_assignment_user_shift", "user_id", "shift_id"),
        Index("idx_assignment_status", "status"),
        Index("idx_assignment_hour", "hour_index"),
        Index("idx_assignment_covering", "covering_for_user_id"),
        UniqueConstraint("user_id", "shift_id", "hour_index", name="uq_user_shift_hour"),
    )
    
    def __repr__(self):
        return f"<Assignment(id={self.id}, user_id={self.user_id}, task_name={self.task_name}, status={self.status.value})>"


class ApprovalRequest(Base):
    """Requests requiring admin approval"""
    __tablename__ = "approval_requests"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    assignment_id = Column(Integer, ForeignKey("assignments.id"), nullable=False)
    type = Column(Enum(ApprovalType), nullable=False)
    requested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    payload = Column(JSON, nullable=True, default=dict)  # Request-specific data
    status = Column(Enum(ApprovalStatus), nullable=False, default=ApprovalStatus.PENDING)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolver_id = Column(String, ForeignKey("users.id"), nullable=True)  # Admin who resolved
    resolver_note = Column(Text, nullable=True)
    
    # Relationships
    user = relationship("User", back_populates="approval_requests", foreign_keys=[user_id])
    resolver = relationship("User", foreign_keys=[resolver_id])
    assignment = relationship("Assignment", back_populates="approval_requests")
    
    # Indexes
    __table_args__ = (
        Index("idx_approval_status", "status"),
        Index("idx_approval_user_assignment", "user_id", "assignment_id"),
        Index("idx_approval_requested_at", "requested_at"),
    )
    
    def __repr__(self):
        return f"<ApprovalRequest(id={self.id}, type={self.type.value}, status={self.status.value})>"


class Settings(Base):
    """Global bot configuration"""
    __tablename__ = "settings"
    
    id = Column(Integer, primary_key=True, default=1)  # Singleton row
    assignments_channel_id = Column(String, nullable=True)  # Parent channel for threads
    admin_channel_id = Column(String, nullable=True)        # Admin notifications
    operator_role_id = Column(String, nullable=True)        # @Operator role
    admin_role_id = Column(String, nullable=True)           # Admin role (optional)
    timezone = Column(String, default="America/Los_Angeles", nullable=False)
    min_on_duty = Column(Integer, default=3, nullable=False)
    cooldown_edit_sec = Column(Integer, default=300, nullable=False)      # 5 minutes
    cooldown_end_early_sec = Column(Integer, default=300, nullable=False) # 5 minutes
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    def __repr__(self):
        return f"<Settings(timezone={self.timezone}, min_on_duty={self.min_on_duty})>"


class AuditLog(Base):
    """Comprehensive audit trail"""
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    actor_id = Column(String, nullable=True)  # User who performed the action (null for system)
    action = Column(String, nullable=False)   # Action type
    target = Column(String, nullable=True)    # Target of the action
    metadata = Column(JSON, nullable=True, default=dict)  # Action-specific data
    
    # Indexes
    __table_args__ = (
        Index("idx_audit_at", "at"),
        Index("idx_audit_actor", "actor_id"),
        Index("idx_audit_action", "action"),
    )
    
    def __repr__(self):
        return f"<AuditLog(id={self.id}, action={self.action}, actor_id={self.actor_id})>"


class DashState(Base):
    """Dashboard state tracking"""
    __tablename__ = "dash_state"
    
    id = Column(Integer, primary_key=True, default=1)  # Singleton row
    dashboard_message_id = Column(String, nullable=True)  # Discord message ID
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    def __repr__(self):
        return f"<DashState(dashboard_message_id={self.dashboard_message_id})>"


# Helper functions for database operations
def get_or_create_user(db: Session, user_id: str, display_name: str, is_operator: bool = False, is_admin: bool = False) -> User:
    """Get existing user or create new one"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        user = User(
            id=user_id,
            display_name=display_name,
            is_operator=is_operator,
            is_admin=is_admin
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Update display name if changed
        if user.display_name != display_name:
            user.display_name = display_name
            db.commit()
    
    return user


def get_active_shift(db: Session, user_id: str) -> Optional[Shift]:
    """Get user's currently active shift"""
    return db.query(Shift).filter(
        Shift.user_id == user_id,
        Shift.end_at.is_(None)
    ).first()


def get_settings(db: Session) -> Settings:
    """Get global settings (create if not exists)"""
    settings = db.query(Settings).first()
    if not settings:
        settings = Settings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def log_action(db: Session, action: str, actor_id: Optional[str] = None, target: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
    """Create audit log entry"""
    log_entry = AuditLog(
        actor_id=actor_id,
        action=action,
        target=target,
        metadata=metadata or {}
    )
    db.add(log_entry)
    db.commit()
