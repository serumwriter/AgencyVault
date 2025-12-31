class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)

    full_name = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)
    email = Column(String(255), nullable=True)

    source = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)

    status = Column(String(50), default="New")

    product_interest = Column(String(50), default="UNKNOWN")
    ai_confidence = Column(Integer, nullable=True)
    ai_evidence = Column(Text, nullable=True)
    ai_summary = Column(Text, nullable=True)
    needs_human = Column(Integer, default=0)

    attempt_count = Column(Integer, default=0)
    last_contacted_at = Column(DateTime, nullable=True)
    next_followup_at = Column(DateTime, nullable=True)

    state = Column(String, nullable=True)
    dob = Column(String, nullable=True)
    smoker = Column(String, nullable=True)
    height = Column(String, nullable=True)
    weight = Column(String, nullable=True)
    desired_coverage = Column(String, nullable=True)
    monthly_budget = Column(String, nullable=True)
    time_horizon = Column(String, nullable=True)
    health_notes = Column(Text, nullable=True)

    do_not_contact = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)



from .database import engine, Base

Base.metadata.create_all(bind=engine)
