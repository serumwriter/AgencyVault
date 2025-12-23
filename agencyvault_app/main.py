import os
import re
import csv
from datetime import datetime

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import sessionmaker, declarative_base

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

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    if not logged_in(request):
        return RedirectResponse("/login")

    db = SessionLocal()
    leads = db.query(Lead).order_by(Lead.id.desc()).all()
    db.close()

    rows = "".join(
        f"<div class='card'><b>{l.full_name}</b><br>{l.phone}<br>{l.email or ''}</div>"
        for l in leads
    )

    return page("Dashboard", f"""
    <div class="card">
      <h3>Add Lead</h3>
      <form method="post" action="/leads/create">
        <input name="name" placeholder="Full Name" required>
        <input name="phone" placeholder="Phone" required>
        <input name="email" placeholder="Email">
        <button>Add Lead</button>
      </form>
    </div>

    <div class="card">
      <h3>Bulk Upload (CSV)</h3>
      <form method="post" action="/leads/upload" enctype="multipart/form-data">
        <input type="file" name="file" accept=".csv" required>
        <button>Upload</button>
      </form>
    </div>

    {rows}
    """)

@app.post("/leads/create")
def create_lead(name: str = Form(...), phone: str = Form(...), email: str = Form("")):
    db = SessionLocal()
    db.add(Lead(
        full_name=name,
        phone=normalize_phone(phone),
        email=email or None
    ))
    db.commit()
    db.close()
    return RedirectResponse("/dashboard", status_code=302)

@app.post("/leads/upload")
def upload(file: UploadFile = File(...)):
    raw = file.file.read().decode("utf-8", errors="ignore").splitlines()
    rows = [r for r in csv.reader(raw) if any(c.strip() for c in r)]

    if not rows:
        return HTMLResponse(page("Upload Error", "<p>Empty file</p><a href='/dashboard'>Back</a>"))

    mapping = infer_mapping(rows)

    db = SessionLocal()
    imported = 0
    skipped = 0

    for r in rows:
        name = r[mapping["name"]].strip() if mapping.get("name") is not None and mapping["name"] < len(r) else ""
        phone = r[mapping["phone"]].strip() if mapping.get("phone") is not None and mapping["phone"] < len(r) else ""
        email = r[mapping["email"]].strip() if mapping.get("email") is not None and mapping["email"] < len(r) else ""

        # ABSOLUTE RULE: do not guess
        if not looks_like_name(name):
            skipped += 1
            continue

        if not looks_like_phone(phone):
            skipped += 1
            continue

        db.add(Lead(
            full_name=name,
            phone=normalize_phone(phone),
            email=email or None,
            status="new"
        ))
        imported += 1

    db.commit()
    db.close()

    return HTMLResponse(
        page(
            "Upload Complete",
            f"""
            <h3>Imported: {imported}</h3>
            <p>Skipped (non-human / low confidence): {skipped}</p>
            <a href="/dashboard">Back</a>
            """
        )
    )
