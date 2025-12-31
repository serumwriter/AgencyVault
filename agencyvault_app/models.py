from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)

    full_name = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)
    email = Column(String(255), nullable=True)

    source = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)

    status = Column(String(50), default="New")

    # AI guardrails
    product_interest = Column(String(50), default="UNKNOWN")
    ai_confidence = Column(Integer, nullable=True)
    ai_evidence = Column(Text, nullable=True)
    ai_summary = Column(Text, nullable=True)
    needs_human = Column(Integer, default=0)

    # Pre-qual
    state = Column(String(50), nullable=True)
    dob = Column(String(50), nullable=True)
    smoker = Column(String(50), nullable=True)
    height = Column(String(50), nullable=True)
    weight = Column(String(50), nullable=True)
    desired_coverage = Column(String(50), nullable=True)
    monthly_budget = Column(String(50), nullable=True)
    time_horizon = Column(String(50), nullable=True)
    health_notes = Column(Text, nullable=True)

    # Ops
    do_not_contact = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
