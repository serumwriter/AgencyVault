print(">>> LOADED agencyvault_app/main.py <<<")

TWILIO_ENABLED = true

import os
import re
import csv
from datetime import datetime

from fastapi import FastAPI, Request, Form, UploadFile, File, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import sessionmaker, declarative_base
if TWILIO_ENABLED:
    from twilio_client import place_call






# -------------------------------------------------
# CONFIG
# -------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

DATABASE_URL = DATABASE_URL.replace(
    "postgresql://", "postgresql+psycopg://"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# -------------------------------------------------
# MODELS
# -------------------------------------------------

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    full_name = Column(String(255), nullable=False)
    phone = Column(String(50), nullable=False, index=True)
    email = Column(String(255))

    dob = Column(String(20))
    age = Column(Integer)

    city = Column(String(100))
    county = Column(String(100))
    state = Column(String(10))
    zip = Column(String(20))

    lead_type = Column(String(50))

    dial_score = Column(Integer, default=0)
    dial_status = Column(String(20), default="HOLD")

    # ✅ AI decision (safe, no external deps)
    ai_decision = Column(String(50), default="unprocessed")

    notes = Column(Text)
    status = Column(String(50), default="new")
    created_at = Column(DateTime, default=datetime.utcnow)





Base.metadata.create_all(bind=engine)

# -------------------------------------------------
# APP
# -------------------------------------------------

app = FastAPI(title="AgencyVault")
app.add_middleware(SessionMiddleware, secret_key="CHANGE_ME")

# -------------------------------------------------
# HELPERS (NO INDENTATION GAMES)
# -------------------------------------------------

def normalize_phone(s):
    d = re.sub(r"\D", "", s or "")
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    if d.startswith("+"):
        return d
    return d

def looks_like_phone(s):
    return len(re.sub(r"\D", "", s or "")) in (10, 11)

def looks_like_email(s):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s or ""))

def looks_like_name(s):
    if not s:
        return False

    s = s.strip()
    s_lower = s.lower()

    # Hard reject: lead categories, products, sources
    banned = [
        "lead", "aged", "internet",
        "gold", "silver", "bronze",
        "final", "expense", "finalexpense", "fex", "f.e.x",
        "iul", "term", "whole", "life",
        "vet", "veteran", "mortgage",
        "facebook", "fb", "tiktok", "tt"
    ]

    if any(b in s_lower for b in banned):
        return False

    parts = s.split()

    # Human names are usually 2–4 words
    if not (2 <= len(parts) <= 4):
        return False

    # Reject if any word is all-caps (common for counties/cities)
    if any(p.isupper() for p in parts):
        return False

    # Reject if it contains location words
    location_words = [
        "county", "city", "town", "township",
        "parish", "borough", "district"
    ]
    if any(w in s_lower for w in location_words):
        return False

    # Each word must look like a proper name
    for p in parts:
        if not p.isalpha():
            return False
        if not p[0].isupper():
            return False

    return True

    return True

def infer_mapping(rows):
    cols = max(len(r) for r in rows)
    scores = {
        "name": [0] * cols,
        "phone": [0] * cols,
        "email": [0] * cols,
    }

    for r in rows[:50]:
        for i in range(cols):
            if i >= len(r):
                continue
            v = r[i].strip()
            if looks_like_phone(v):
                scores["phone"][i] += 1
            if looks_like_email(v):
                scores["email"][i] += 1
            if looks_like_name(v):
                scores["name"][i] += 1

    def best(key, min_score=1):
        i = max(range(cols), key=lambda x: scores[key][x])
        return i if scores[key][i] >= min_score else None

    return {
        "name": best("name"),
        "phone": best("phone"),
        "email": best("email"),
    }

def logged_in(request):
    return request.cookies.get("av") == "1"

def page(title, body):
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px }}
.card {{ background:#111827;padding:16px;margin:16px 0;border-radius:12px }}
input,button {{ padding:10px;width:100%;margin:6px 0 }}
button {{ background:#2563eb;color:white;border:none }}
</style>
</head>
<body>
{body}
</body>
</html>"""

def simple_ai_decide(lead):
    # Extremely safe, no external calls
    if not lead.phone:
        return "incomplete"

    if lead.age and lead.age < 25:
        return "follow_up"

    return "call_now"

# -------------------------------------------------
# ROUTES
# -------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/")
def root():
    return RedirectResponse("/dashboard")

@app.get("/login", response_class=HTMLResponse)
def login_page():
    return page("Login", """
    <div class="card">
      <form method="post" action="/login">
        <input name="email" placeholder="Email" required>
        <input name="password" placeholder="Password" type="password" required>
        <button type="submit">Login</button>
      </form>
    </div>
    """)

@app.post("/login")
def login():
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie("av", "1")
    return resp

@app.get("/dashboard")
def dashboard():
    db = SessionLocal()
    leads = db.query(Lead).order_by(Lead.created_at.desc()).all()
    db.close()

    rows = ""
    for l in leads:
        rows += f"""
        <div class="card">
          <b>{l.full_name}</b><br>
          {l.phone}<br>
          {l.email or ""}

          <div style="margin-top:6px;">
            <strong>Dial Score:</strong> {l.dial_score}<br>
            <strong>Status:</strong> {l.dial_status}
            <br><strong>AI Decision:</strong> {l.ai_decision}
          </div>

          <form method="post" action="/leads/delete/{l.id}">
            <button style="background:#b91c1c;margin-top:8px">
              Delete
            </button>
          </form>
        </div>
        """

    if not rows:
        rows = "<div class='card'>No leads yet</div>"

    return HTMLResponse(f"""
    <html>
    <head>
      <title>AgencyVault</title>
      <style>
        body {{ background:#0b0b0b; color:#fff; font-family:Arial; }}
        .card {{ background:#151515; padding:16px; margin:12px 0; border-radius:8px; }}
        input, button {{ padding:8px; margin:4px 0; }}
        button {{ cursor:pointer; }}
      </style>
    </head>
    <body>

    <div class="card">
      <div class="card">
  <form method="post" action="/dial/start">
    <button style="background:#2563eb;">Start Dialing</button>
  </form>
</div>

      <h3>Add Lead</h3>
      <form method="post" action="/leads/create">
        <input name="name" placeholder="Full Name" required><br>
        <input name="phone" placeholder="Phone" required><br>
        <input name="email" placeholder="Email"><br>
        <button>Add Lead</button>
      </form>
    </div>

    <div class="card">
      <h3>Bulk Upload (CSV)</h3>
      <form method="post" action="/leads/upload" enctype="multipart/form-data">
        <input type="file" name="file" accept=".csv" required><br>
        <button>Upload</button>
      </form>
    </div>

    {rows}

    </body>
    </html>
    """)

@app.post("/dial/start")
def start_dialing():
    db = SessionLocal()

    leads = (
        db.query(Lead)
        .filter(Lead.dial_status == "READY")
        .order_by(Lead.dial_score.desc())
        .limit(5)
        .all()
    )

    # Always do the queueing (safe)
    for i, lead in enumerate(leads, start=1):
        lead.dial_queue_position = i

    # Only place calls if Twilio is enabled
    if TWILIO_ENABLED and leads:
        try:
            base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
            if not base_url:
                raise RuntimeError("PUBLIC_BASE_URL not set")

            twiml_url = f"{base_url}/twiml/placeholder"

            # Call ONLY the first lead for now (safe)
            lead = leads[0]
            place_call(lead.phone, twiml_url)
            lead.dial_status = "CALLED"

        except Exception as e:
            print("Dialing failed:", e)

    db.commit()
    db.close()

    return RedirectResponse("/dashboard", status_code=302)




@app.post("/leads/create")
def create_lead(name: str = Form(...), phone: str = Form(...), email: str = Form("")):
    db = SessionLocal()

    lead = Lead(
        full_name=name,
        phone=normalize_phone(phone),
        email=email or None
    )

    # ✅ AI decision happens automatically
    lead.ai_decision = simple_ai_decide(lead)

    db.add(lead)
    db.commit()
    db.close()

    return RedirectResponse("/dashboard", status_code=302)


@app.post("/leads/delete/{lead_id}")
def delete_lead(lead_id: int):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if lead:
        db.delete(lead)
        db.commit()
    db.close()
    return RedirectResponse("/dashboard", status_code=302)


@app.post("/leads/upload")
def upload(file: UploadFile = File(...)):
    raw = file.file.read().decode("utf-8", errors="ignore").splitlines()
    rows = [r for r in csv.reader(raw) if any(c.strip() for c in r)]

    if not rows:
        return HTMLResponse(
            page("Upload Error", "<p>Empty file</p><a href='/dashboard'>Back</a>")
        )

    mapping = infer_mapping(rows)

    db = SessionLocal()
    imported = 0
    skipped = 0

    for r in rows:
        name = (
            r[mapping["name"]].strip()
            if mapping.get("name") is not None and mapping["name"] < len(r)
            else ""
        )
        phone = (
            r[mapping["phone"]].strip()
            if mapping.get("phone") is not None and mapping["phone"] < len(r)
            else ""
        )
        email = (
            r[mapping["email"]].strip()
            if mapping.get("email") is not None and mapping["email"] < len(r)
            else ""
        )

        if not looks_like_name(name):
            skipped += 1
            continue

        if not looks_like_phone(phone):
            skipped += 1
            continue

        dob = None
        age = None
        city = None
        state = None
        zip_code = None
        lead_type = None
        notes = []

        for cell in r:
            c = cell.strip()
            if not c:
                continue

            if looks_like_dob(c):
                dob = c
                notes.append(f"DOB: {c}")
            elif looks_like_age(c):
                age = int(c)
                notes.append(f"Age: {c}")
            elif looks_like_zip(c):
                zip_code = c
            elif looks_like_state(c):
                state = c.upper()
            elif looks_like_city(c):
                city = c
            elif looks_like_lead_type(c):
                lead_type = c.upper()

        db.add(
            Lead(
                full_name=name,
                phone=normalize_phone(phone),
                email=email or None,
                dob=dob,
                age=age,
                city=city,
                state=state,
                zip=zip_code,
                lead_type=lead_type,
                notes="; ".join(notes) if notes else None,
                status="new",
            )
        )
        imported += 1

    db.commit()
    db.close()

    return HTMLResponse(
        page(
            "Upload Complete",
            f"""
            <h3>Imported: {imported}</h3>
            <p>Skipped: {skipped}</p>
            <a href="/dashboard">Back</a>
            """,
        )
    )


def looks_like_age(v):
    return v.isdigit() and 18 <= int(v) <= 110

def looks_like_zip(v):
    return v.isdigit() and len(v) == 5

def looks_like_state(v):
    return v.isalpha() and len(v) == 2

def looks_like_city(v):
    return v.replace(" ", "").isalpha() and len(v) > 2

def looks_like_lead_type(v):
    v = v.lower()
    return any(x in v for x in ["fex", "final", "aged", "vet", "veteran"])

from fastapi.responses import RedirectResponse

@app.post("/dial/start")
def start_dialing():
    db = SessionLocal()

    leads = (
        db.query(Lead)
        .filter(Lead.dial_status == "READY")
        .order_by(Lead.dial_score.desc())
        .limit(10)
        .all()
    )

    for i, lead in enumerate(leads, start=1):
        lead.dial_queue_position = i

    db.commit()
    db.close()

    return RedirectResponse("/dashboard", status_code=302)

