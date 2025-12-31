# models.py (ROOT)
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    Text,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# Keep enum-like values as strings (simple + migration-friendly)
LEAD_STATES = ("NEW", "WORKING", "CONTACTED", "CLOSED", "DO_NOT_CONTACT")
TASK_TYPES = ("CALL", "TEXT", "EMAIL", "REVIEW", "FOLLOWUP")
TASK_STATUS = ("PENDING", "DONE", "CANCELED")
ACTION_TYPES = ("CALL_PREP", "SMS_SEND", "EMAIL_SEND", "FOLLOWUP_SCHEDULE", "STATUS_UPDATE", "NOTE_ADD")
ACTION_STATUS = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED")
RUN_STATUS = ("STARTED", "SUCCEEDED", "FAILED")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    phone: Mapped[str] = mapped_column(String(50), default="", nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    state: Mapped[str] = mapped_column(String(30), default="NEW", nullable=False, index=True)

    dial_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_contacted_at = Column(DateTime, nullable=True)

    tasks: Mapped[list["Task"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    actions: Mapped[list["Action"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    memory: Mapped[list["LeadMemory"]] = relationship(back_populates="lead", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Lead id={self.id} name={self.full_name!r} phone={self.phone!r} state={self.state!r}>"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)

    type: Mapped[str] = mapped_column(String(30), default="CALL", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False, index=True)

    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False, index=True)  # 0=highest
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    lead: Mapped["Lead"] = relationship(back_populates="tasks")


class Action(Base):
    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)

    type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False, index=True)

    # execution controls / routing (twilio, email provider, etc.)
    tool: Mapped[str] = mapped_column(String(50), default="", nullable=False)

    # payload is text for now (JSON string). Keeps dependencies minimal.
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    lead: Mapped["Lead"] = relationship(back_populates="actions")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(30), default="planning", nullable=False, index=True)  # planning/execution
    status: Mapped[str] = mapped_column(String(20), default="STARTED", nullable=False, index=True)

    batch_size: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LeadMemory(Base):
    __tablename__ = "lead_memory"
    __table_args__ = (
        UniqueConstraint("lead_id", "key", name="uq_lead_memory_lead_key"),
        Index("ix_lead_memory_lead_id_key", "lead_id", "key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)

    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    lead: Mapped["Lead"] = relationship(back_populates="memory")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    event: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
