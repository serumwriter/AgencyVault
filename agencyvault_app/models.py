from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime

from .database import Base


# =========================
# USER (AGENT / OWNER)
# =========================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    full_name = Column(String(255), nullable=True)
    password_hash = Column(String(255), nullable=False)

    leads = relationship(
        "Lead",
        back_populates="owner",
        cascade="all, delete-orphan"
    )


# =========================
# LEAD (LIFE INSURANCE)
# =========================
class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)

    # Ownership
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Contact info
    full_name = Column(String(255), nullable=False)
    phone = Column(String(50), nullable=True)
    email = Column(String(255), nullable=True)
    source = Column(String(255), nullable=True)

    # Basic CRM
    status = Column(String(50), default="New")
    notes = Column(Text, nullable=True)

    # =========================
    # AI EMPLOYEE MEMORY
    # =========================
    call_status = Column(String(50), default="new")
    attempt_count = Column(Integer, default=0)

    last_contacted_at = Column(DateTime, nullable=True)
    next_followup_at = Column(DateTime, nullable=True)

    qualification_score = Column(Integer, nullable=True)
    primary_objection = Column(String(255), nullable=True)

    ai_summary = Column(Text, nullable=True)

    # Appointment
    appointment_time = Column(DateTime, nullable=True)
    appointment_type = Column(
        String(100),
        default="life insurance enrollment"
    )

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

    owner = relationship("User", back_populates="leads")

