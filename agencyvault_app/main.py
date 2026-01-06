import csv
import io
import json
import os
import re
from datetime import datetime
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .database import engine, SessionLocal
from .models import Base, Lead, Action, AgentRun, LeadMemory, AuditLog, Message
from .image_import import extract_text_from_image_bytes, parse_leads_from_text, extract_text_from_pdf_bytes
from .google_drive_import import import_google_sheet, import_drive_csv, import_google_doc_text
from .twilio_client import send_alert_sms, send_lead_sms, make_call

app = FastAPI(title="AgencyVault Command Center")

# =========================
# Startup
# =========================
@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)

# =========================
# Sanitization
# =========================
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

BAD_NAME_WORDS = {
    "lead", "bronze", "silver", "gold", "ethos",
    "facebook", "insurance", "prospect", "unknown",
    "meta", "client", "customer", "applicant"
}

def now():
    return datetime.utcnow()

def clean_text(val):
    if val is None:
        return None
    return CONTROL_RE.sub("", str(val)).replace("\x00", "").strip() or None

def normalize_phone(val):
    val = clean_text(val) or ""
    digits = re.sub(r"\D", "", val)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None

def safe_first_name(full_name):
    if not full_name:
        return ""
    first = full_name.strip().split()[0].lower()
    if first in BAD_NAME_WORDS or len(first) < 2:
        return ""
    return first.capitalize()

def dedupe_exists(db, phone, email):
    if phone and db.query(Lead).filter(Lead.phone == phone).first():
        return True
    if email and db.query(Lead).filter(Lead.email == email).first():
        return True
    return False

def log_event(db, lead_id, run_id, event, detail):
    db.add(AuditLog(
        lead_id=lead_id,
        run_id=run_id,
        event=event,
        detail=(detail or "")[:5000],
        created_at=now()
    ))

def mem_get(db, lead_id, key):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    return row.value if row else None

def mem_set(db, lead_id, key, value):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    if row:
        row.value = value
        row.updated_at = now()
    else:
        db.add(LeadMemory(
            lead_id=lead_id,
            key=key,
            value=value,
            updated_at=now()
        ))

def require_admin(req):
    token = req.headers.get("x-admin-token") or req.query_params.get("token") or ""
    want = (os.getenv("ADMIN_TOKEN") or "").strip()
    return want and token.strip() == want

# =========================
# Health
# =========================
@app.get("/health")
def health():
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    return {"ok": True}

@app.get("/")
def root():
    return RedirectResponse("/dashboard")

# =========================
# Dashboard
# =========================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    try:
        total = db.query(Lead).count()
        new = db.query(Lead).filter(Lead.state == "NEW").count()
        working = db.query(Lead).filter(Lead.state == "WORKING").count()
        contacted = db.query(Lead).filter(Lead.state == "CONTACTED").count()
        dnc = db.query(Lead).filter(Lead.state == "DO_NOT_CONTACT").count()
        pending = db.query(Action).filter(Action.status == "PENDING").count()

        logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(20).all()
        feed = "".join(
            f"<div><b>{l.event}</b><br>{l.detail}<hr></div>"
            for l in logs
        )

        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(10).all()
        leads_html = "".join(
            f"""
            <div>
              <b>#{l.id} {l.full_name or "Unknown"}</b><br>
              Phone: {l.phone or "-"} | Email: {l.email or "-"}<br>
              State: {l.state}
              <hr>
            </div>
            """ for l in leads
        )

        return HTMLResponse(f"""
        <html>
        <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
          <h1>AgencyVault Command Center</h1>

          <p>
            Total: {total}<br>
            NEW: {new}<br>
            WORKING: {working}<br>
            CONTACTED: {contacted}<br>
            DNC: {dnc}<br>
            Pending Actions: {pending}
          </p>

          <h2>Newest Leads</h2>
          {leads_html or "No leads"}

          <h2>Activity</h2>
          {feed or "No activity"}

          <hr>
          <a href="/leads">All Leads</a> |
          <a href="/leads/new">Add Lead</a> |
          <a href="/imports">Imports</a> |
          <a href="/actions">Actions</a> |
          <a href="/activity">Activity Log</a> |
          <a href="/admin">Admin</a>
        </body>
        </html>
        """)
    finally:
        db.close()

# =========================
# Leads
# =========================
@app.get("/leads", response_class=HTMLResponse)
def leads():
    db = SessionLocal()
    try:
        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(200).all()
        rows = "".join(
            f"""
            <div>
              <b>#{l.id} {l.full_name or "Unknown"}</b><br>
              Phone: {l.phone or "-"} | Email: {l.email or "-"}<br>
              State: {l.state}
              <hr>
            </div>
            """ for l in leads
        )
        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
        <a href="/dashboard">Back</a>
        <h2>All Leads</h2>
        {rows or "No leads"}
        </body></html>
        """)
    finally:
        db.close()

@app.get("/leads/new", response_class=HTMLResponse)
def leads_new():
    return HTMLResponse("""
    <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
    <a href="/dashboard">Back</a>
    <h2>Add Lead</h2>
    <form method="post">
      Name:<br><input name="full_name"><br><br>
      Phone:<br><input name="phone"><br><br>
      Email:<br><input name="email"><br><br>
      <button type="submit">Create</button>
    </form>
    </body></html>
    """)

@app.post("/leads/new")
def leads_new_post(
    full_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form("")
):
    db = SessionLocal()
    try:
        p = normalize_phone(phone)
        e = clean_text(email)
        n = clean_text(full_name) or "Unknown"

        if not p:
            return HTMLResponse("Invalid phone", status_code=400)
        if dedupe_exists(db, p, e):
            return HTMLResponse("Duplicate lead", status_code=409)

        db.add(Lead(
            full_name=n,
            phone=p,
            email=e,
            state="NEW",
            created_at=now(),
            updated_at=now()
        ))
        log_event(db, None, None, "LEAD_CREATED", f"{n} {p}")
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()

# =========================
# Admin
# =========================
@app.get("/admin", response_class=HTMLResponse)
def admin():
    return HTMLResponse("""
    <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
    <a href="/dashboard">Back</a>
    <h2>Admin</h2>
    <form method="post" action="/admin/wipe">
      <input name="confirm" placeholder="DELETE ALL LEADS">
      <button type="submit">Delete All</button>
    </form>
    </body></html>
    """)

@app.post("/admin/wipe")
def admin_wipe(request: Request, confirm: str = Form(...)):
    if confirm != "DELETE ALL LEADS":
        return JSONResponse({"error": "Confirmation mismatch"}, status_code=400)
    if not require_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM messages"))
        db.execute(text("DELETE FROM audit_log"))
        db.execute(text("DELETE FROM lead_memory"))
        db.execute(text("DELETE FROM actions"))
        db.execute(text("DELETE FROM agent_runs"))
        db.execute(text("DELETE FROM leads"))
        db.commit()
        return {"ok": True}
    finally:
        db.close()
