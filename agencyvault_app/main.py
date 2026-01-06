# agencyvault_app/main.py
import csv
import io
import json
import os
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, UploadFile, File, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy import text, or_
from sqlalchemy.orm import Session

from .database import engine, SessionLocal
from .models import Base, Lead, Action, AgentRun, LeadMemory, AuditLog, Message
from .google_drive_import import import_google_sheet, import_drive_csv, import_google_doc_text
from .image_import import extract_text_from_image_bytes, parse_leads_from_text
from .twilio_client import send_alert_sms, send_lead_sms, make_call

app = FastAPI(title="AgencyVault — AI Command Center")

# ============================================================
# STARTUP
# ============================================================
@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)

# ============================================================
# UTILITIES
# ============================================================
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
BAD_NAME_WORDS = {"lead","test","unknown","facebook","insurance","prospect"}

def now():
    return datetime.utcnow()

def clean_text(v):
    if not v:
        return None
    return CONTROL_RE.sub("", str(v)).strip() or None

def normalize_phone(v):
    if not v:
        return None
    d = re.sub(r"\D", "", v)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return None

def safe_first_name(name: str) -> str:
    if not name:
        return ""
    f = name.split()[0].lower()
    if f in BAD_NAME_WORDS or len(f) < 2:
        return ""
    return f.capitalize()

def log(db: Session, lead_id, run_id, event, detail):
    db.add(AuditLog(
        lead_id=lead_id,
        run_id=run_id,
        event=event,
        detail=(detail or "")[:5000],
        created_at=now()
    ))

# ============================================================
# HEALTH
# ============================================================
@app.get("/health")
def health():
    with engine.begin() as c:
        c.execute(text("SELECT 1"))
    return {"ok": True}

@app.get("/")
def root():
    return RedirectResponse("/dashboard")

# ============================================================
# DASHBOARD (PRO UI)
# ============================================================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    try:
        total = db.query(Lead).count()
        new = db.query(Lead).filter(Lead.state == "NEW").count()
        working = db.query(Lead).filter(Lead.state == "WORKING").count()
        pending = db.query(Action).filter(Action.status == "PENDING").count()

        recent = db.query(Lead).order_by(Lead.created_at.desc()).limit(8).all()

        cards = ""
        for l in recent:
            cards += f"""
            <div class="card">
              <b>{l.full_name or "Unknown"}</b>
              <div class="muted">{l.phone or "—"}</div>
              <a class="btn sm" href="/leads/{l.id}">Open</a>
            </div>
            """

        return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgencyVault</title>
<style>
body {{
  background:#0b0f17;color:#e6edf3;font-family:system-ui;
  margin:0;padding:20px;
}}
.top {{
  display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;
}}
.btn {{
  background:#111827;border:1px solid #233044;color:#e6edf3;
  padding:10px 14px;border-radius:12px;text-decoration:none;
}}
.btn.sm {{ padding:6px 10px;font-size:13px; }}
.grid {{
  display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:18px;
}}
.panel {{
  background:#0f1624;border:1px solid #233044;border-radius:18px;padding:16px;
}}
.stat {{ font-size:28px;font-weight:800; }}
.muted {{ opacity:.7;font-size:13px; }}
.cards {{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
  gap:12px;margin-top:12px;
}}
.card {{
  background:#0b1220;border:1px solid #1f2b3e;border-radius:14px;padding:12px;
}}
textarea {{
  width:100%;background:#0b0f17;color:#e6edf3;
  border:1px solid #233044;border-radius:12px;padding:12px;
}}
</style>
</head>

<body>

<div class="top">
  <h1>AgencyVault</h1>
  <div>
    <a class="btn" href="/leads">📇 Leads</a>
    <a class="btn" href="/leads/new">➕ Add Lead</a>
    <a class="btn" href="/imports">⬆ Import</a>
    <a class="btn" href="/actions">⚙ Actions</a>
  </div>
</div>

<div class="grid">
  <div class="panel"><div class="stat">{total}</div><div class="muted">Total Leads</div></div>
  <div class="panel"><div class="stat">{new}</div><div class="muted">New</div></div>
  <div class="panel"><div class="stat">{working}</div><div class="muted">Working</div></div>
  <div class="panel"><div class="stat">{pending}</div><div class="muted">Pending Actions</div></div>
</div>

<div class="grid" style="grid-template-columns:2fr 1fr">
  <div class="panel">
    <h3>🤖 AI Employee</h3>
    <textarea id="msg" placeholder="Ask anything about your leads..."></textarea>
    <button class="btn sm" onclick="send()">Send</button>
    <pre id="out" class="muted"></pre>
  </div>

  <div class="panel">
    <h3>🕒 Recent Leads</h3>
    <div class="cards">{cards or "<div class='muted'>None</div>"}</div>
  </div>
</div>

<script>
async function send() {{
  const m = document.getElementById("msg").value;
  document.getElementById("out").textContent = "Thinking...";
  const r = await fetch("/api/assistant", {{
    method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body:JSON.stringify({{message:m}})
  }});
  const d = await r.json();
  document.getElementById("out").textContent = d.reply || "OK";
}}
</script>

</body>
</html>
        """)
    finally:
        db.close()

# ============================================================
# AI ASSISTANT (FREE FALLBACK)
# ============================================================
@app.post("/api/assistant")
async def assistant(payload: dict):
    msg = (payload.get("message") or "").strip()
    db = SessionLocal()
    try:
        if not msg:
            return {"reply": "Ask me about leads, follow-ups, or what to do next."}

        # FREE intelligence fallback
        if "what should i do" in msg.lower():
            new = db.query(Lead).filter(Lead.state == "NEW").count()
            return {"reply": f"You have {new} new leads. Run outreach and focus on replies first."}

        return {"reply": "AI online. You can ask about leads, counts, or next steps."}
    finally:
        db.close()

# ============================================================
# LEADS LIST + SEARCH
# ============================================================
@app.get("/leads", response_class=HTMLResponse)
def leads_list(q: str = Query("", alias="search")):
    db = SessionLocal()
    try:
        qry = db.query(Lead)
        if q:
            qry = qry.filter(or_(
                Lead.full_name.ilike(f"%{q}%"),
                Lead.phone.ilike(f"%{q}%"),
                Lead.email.ilike(f"%{q}%"),
            ))
        leads = qry.order_by(Lead.created_at.desc()).limit(200).all()

        rows = ""
        for l in leads:
            rows += f"""
            <tr>
              <td>{l.id}</td>
              <td>{l.full_name}</td>
              <td>{l.phone}</td>
              <td>{l.state}</td>
              <td><a href="/leads/{l.id}">Open</a></td>
            </tr>
            """

        return HTMLResponse(f"""
<html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
<a href="/dashboard">← Back</a>
<h2>All Leads</h2>
<form>
<input name="search" placeholder="Search..." value="{q}">
<button>Search</button>
</form>
<table width="100%" cellpadding="8">
<tr><th>ID</th><th>Name</th><th>Phone</th><th>Status</th><th></th></tr>
{rows}
</table>
</body></html>
        """)
    finally:
        db.close()

# ============================================================
# LEAD DETAIL (IMPROVED)
# ============================================================
@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return HTMLResponse("Not found", status_code=404)

        msgs = db.query(Message).filter(Message.lead_id == lead_id).order_by(Message.created_at.desc()).limit(50).all()

        msg_html = "".join(
            f"<div><b>{m.direction}</b>: {m.body}</div>" for m in msgs
        ) or "<div class='muted'>No messages</div>"

        return HTMLResponse(f"""
<html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
<a href="/leads">← Back</a>
<h2>{lead.full_name}</h2>
<div>📞 {lead.phone}</div>
<div>Status: {lead.state}</div>

<form method="post" action="/leads/delete/{lead.id}"
 onsubmit="return confirm('Delete lead?');">
<button style="background:#b91c1c;color:white;padding:10px;border:none;border-radius:10px">
Delete Lead
</button>
</form>

<h3>Messages</h3>
{msg_html}
</body></html>
        """)
    finally:
        db.close()

@app.post("/leads/delete/{lead_id}")
def delete_lead(lead_id: int):
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM leads WHERE id=:id"), {"id": lead_id})
        db.commit()
        return RedirectResponse("/leads", status_code=303)
    finally:
        db.close()
