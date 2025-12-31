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
       # =========================
    # AI DECISION GUARDRAILS
    # =========================
    product_interest = Column(String(50), default="UNKNOWN")  # LIFE, IUL, ANNUITY, UNKNOWN
    ai_confidence = Column(Integer, nullable=True)            # 0-100
    ai_evidence = Column(Text, nullable=True)                 # why AI decided
    needs_human = Column(Integer, default=0)                  # 0/1

    # Pre-qual fields (so AI can prep you)
    dob = Column(String(20), nullable=True)                   # keep as string for now (safe)
    state = Column(String(50), nullable=True)
    smoker = Column(String(10), nullable=True)                # YES/NO/UNKNOWN
    height = Column(String(20), nullable=True)
    weight = Column(String(20), nullable=True)
    health_notes = Column(Text, nullable=True)
    desired_coverage = Column(String(50), nullable=True)      # e.g. 250k, 500k
    monthly_budget = Column(String(50), nullable=True)
    time_horizon = Column(String(50), nullable=True)          # ASAP/30days/just shopping
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

