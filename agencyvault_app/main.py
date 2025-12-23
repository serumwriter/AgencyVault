import os
import re
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Text, text
)
from sqlalchemy.orm import sessionmaker, declarative_base

# -----------------------------
# CONFIG (ONE SOURCE OF TRUTH)
# -----------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "CHANGE_ME_NOW")
APP_TITLE = "AgencyVault"

if not DATABASE_URL:
    # Render will have this set; if not, the app should fail loudly.
    raise RuntimeError("DATABASE_URL is not set")

# SQLAlchemy SYNC engine (stable, simple)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

# -----------------------------
# MODELS
# -----------------------------
class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String(255), nullable=False)
    phone = Column(String(50), nullable=False, index=True)
    email = Column(String(255), nullable=True)
    status = Column(String(50), nullable=False, default="new")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

# Create table if missing (simple + reliable)
Base.metadata.create_all(bind=engine)

# -----------------------------
# APP
# -----------------------------
app = FastAPI(title=APP_TITLE)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# -----------------------------
# HELPERS
# -----------------------------
def normalize_phone(raw: str) -> str:
    """Basic normalization. Keep it simple; improve later."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    # If 10 digits, assume US
    if len(digits) == 10:
        return f"+1{digits}"
    # If 11 and starts with 1
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    # If already looks like +E164
    if raw.startswith("+") and 10 <= len(digits) <= 15:
        return f"+{digits}"
    # fallback: store digits/raw (don’t crash)
    return raw

def require_login(request: Request) -> Optional[RedirectResponse]:
    if not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=302)
    return None

def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; background:#0b0f17; color:#e6edf3; margin:0; padding:0;}}
    .wrap{{max-width:980px; margin:0 auto; padding:24px;}}
    .card{{background:#0f172a; border:1px solid #1f2a44; border-radius:14px; padding:16px; margin:14px 0;}}
    input,button{{padding:10px; border-radius:10px; border:1px solid #2b3a61; background:#0b1220; color:#e6edf3; width:100%;}}
    button{{cursor:pointer; background:#1f6feb; border:none; font-weight:700;}}
    button:hover{{filter:brightness(1.05);}}
    .row{{display:grid; grid-template-columns:1fr 1fr; gap:12px;}}
    .muted{{color:#9fb0c3; font-size:13px;}}
    .lead{{display:flex; justify-content:space-between; gap:14px; align-items:flex-start;}}
    .pill{{display:inline-block; padding:4px 10px; border-radius:999px; background:#111b2e; border:1px solid #243252; font-size:12px; color:#9fb0c3;}}
    a{{color:#7bb0ff; text-decoration:none;}}
    a:hover{{text-decoration:underline;}}
  </style>
</head>
<body>
<div class="wrap">
  {body}
</div>
</body>
</html>"""

# -----------------------------
# HEALTH
# -----------------------------
@app.get("/health")
def health():
    # also checks DB connectivity quickly
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# -----------------------------
# AUTH (SIMPLE)
# -----------------------------
@app.get("/login", response_class=HTMLResponse)
def login_page():
    html = page("Login", f"""
      <h1>AgencyVault</h1>
      <div class="card">
        <p class="muted">Temporary login for build stability. We’ll harden auth next.</p>
        <form method="post" action="/login">
          <div class="row">
            <input name="email" placeholder="Email" required />
            <input name="password" placeholder="Password" type="password" required />
          </div>
          <div style="height:10px"></div>
          <button type="submit">Login</button>
        </form>
      </div>
    """)
    return HTMLResponse(html)

@app.post("/login")
def login(email: str = Form(...), password: str = Form(...)):
    # For now: allow any email/pass to access dashboard during build.
    # We’ll replace with real users table + hashing next.
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie("av_login", "1")
    # session user_id
    # (in real version this is a UUID from users table)
    request_user_id = 1
    # Starlette SessionMiddleware uses request.session, but we don't have Request object here.
    # Workaround: use redirect and set a cookie; require_login checks session; we’ll keep it cookie-based for now.
    # Simpler: mark logged-in in cookie.
    return resp

@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("av_login")
    return resp

def cookie_logged_in(request: Request) -> bool:
    return request.cookies.get("av_login") == "1"

# -----------------------------
# DASHBOARD (STABLE)
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/dashboard", status_code=302)

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    if not cookie_logged_in(request):
        return RedirectResponse("/login", status_code=302)

    db = SessionLocal()
    try:
        leads = db.query(Lead).order_by(Lead.id.desc()).limit(500).all()
    finally:
        db.close()

    lead_items = ""
    for l in leads:
        lead_items += f"""
          <div class="card lead">
            <div>
              <div><strong>{l.full_name}</strong> <span class="pill">{l.status}</span></div>
              <div class="muted">📞 {l.phone} &nbsp; ✉️ {l.email or ""}</div>
              <div class="muted">Added: {l.created_at.strftime("%Y-%m-%d %H:%M")}</div>
            </div>
          </div>
        """

    html = page("Dashboard", f"""
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <h1>Leads</h1>
        <div class="muted"><a href="/logout">Logout</a></div>
      </div>

      <div class="card">
        <h3>Add Lead</h3>
        <form method="post" action="/leads/create">
          <div class="row">
            <input name="name" placeholder="Full Name" required>
            <input name="phone" placeholder="Phone" required>
          </div>
          <div style="height:10px"></div>
          <div class="row">
            <input name="email" placeholder="Email (optional)">
            <input name="notes" placeholder="Notes (optional)">
          </div>
          <div style="height:10px"></div>
          <button type="submit">Add Lead</button>
        </form>
        <p class="muted" style="margin-top:10px;">
          This is the stable base. Next we add bulk upload + AI dialer.
        </p>
      </div>

      {lead_items if lead_items else '<div class="card muted">No leads yet.</div>'}
    """)
    return HTMLResponse(html)

# -----------------------------
# LEAD CREATE (MATCHES UI + API)
# -----------------------------
@app.post("/leads/create")
def lead_create_form(
    request: Request,
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(""),
    notes: str = Form("")
):
    if not cookie_logged_in(request):
        return RedirectResponse("/login", status_code=302)

    db = SessionLocal()
    try:
        l = Lead(
            full_name=name.strip(),
            phone=normalize_phone(phone),
            email=(email.strip() or None),
            notes=(notes.strip() or None),
            status="new",
        )
        db.add(l)
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/dashboard", status_code=302)

# Also accept JSON API posts from anything else
@app.post("/leads")
@app.post("/api/leads")
def lead_create_api(
    full_name: str = Form(None),
    phone: str = Form(None),
    email: str = Form(""),
    name: str = Form(None),
):
    # allow either name or full_name
    n = (full_name or name or "").strip()
    p = (phone or "").strip()
    if not n or not p:
        return JSONResponse({"success": False, "error": "name and phone are required"}, status_code=400)

    db = SessionLocal()
    try:
        l = Lead(full_name=n, phone=normalize_phone(p), email=(email.strip() or None), status="new")
        db.add(l)
        db.commit()
        db.refresh(l)
        return {"success": True, "id": l.id}
    finally:
        db.close()
