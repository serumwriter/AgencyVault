import csv
import io
import json
import os
import re
from datetime import datetime, timedelta

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import text

from .database import engine, SessionLocal
from .models import Base, Lead, Action, AgentRun, LeadMemory, AuditLog, Message
from .ai_employee import plan_actions
from .google_drive_import import import_google_sheet, import_drive_csv, import_google_doc_text
from .image_import import extract_text_from_image_bytes, parse_leads_from_text
from .twilio_client import lead_id_for_call_sid, send_alert_sms

app = FastAPI(title="AgencyVault")

# ---------------------------
# Startup: create schema
# ---------------------------
@app.on_event("startup")
def _startup():
    Base.metadata.create_all(bind=engine)

# ---------------------------
# Strict sanitization / normalization (prevents corruption)
# ---------------------------
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

def clean_text(val):
    if val is None:
        return None
    return _CONTROL_RE.sub("", str(val)).replace("\x00", "").strip() or None

def normalize_phone(val):
    val = clean_text(val) or ""
    d = re.sub(r"\D", "", val)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return None

def dedupe_exists(db: Session, phone: str | None, email: str | None) -> bool:
    if phone and db.query(Lead).filter(Lead.phone == phone).first():
        return True
    if email and db.query(Lead).filter(Lead.email == email).first():
        return True
    return False

def _now():
    return datetime.utcnow()

def _log(db: Session, lead_id: int | None, run_id: int | None, event: str, detail: str):
    db.add(AuditLog(lead_id=lead_id, run_id=run_id, event=event, detail=(detail or "")[:5000]))

def mem_set(db: Session, lead_id: int, key: str, value: str):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    if row:
        row.value = value
        row.updated_at = _now()
    else:
        db.add(LeadMemory(lead_id=lead_id, key=key, value=value, updated_at=_now()))

def cancel_pending_actions(db: Session, lead_id: int, reason: str):
    # Enterprise safety: stop future outreach immediately
    actions = db.query(Action).filter(Action.lead_id == lead_id, Action.status == "PENDING").all()
    for a in actions:
        a.status = "SKIPPED"
        a.error = f"Canceled: {reason}"
        a.finished_at = _now()
    _log(db, lead_id, None, "ACTIONS_CANCELED", reason)

# ---------------------------
# Health
# ---------------------------
@app.get("/health")
def health():
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    return {"ok": True}

@app.get("/")
def root():
    return RedirectResponse("/dashboard")

# ---------------------------
# COMMAND CENTER DASHBOARD (amazing front page)
# ---------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    try:
        # Metrics
        total_leads = db.query(Lead).count()
        new_leads = db.query(Lead).filter(Lead.state == "NEW").count()
        working = db.query(Lead).filter(Lead.state == "WORKING").count()
        dnc = db.query(Lead).filter(Lead.state == "DO_NOT_CONTACT").count()

        pending_actions = db.query(Action).filter(Action.status == "PENDING").count()
        recent_logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(12).all()

        # Hot leads: replied IN and not DNC, last 24h
        since = _now() - timedelta(hours=24)
        hot = (
            db.query(Lead)
            .join(Message, Message.lead_id == Lead.id)
            .filter(Message.direction == "IN", Message.created_at >= since, Lead.state != "DO_NOT_CONTACT")
            .order_by(Message.created_at.desc())
            .limit(8)
            .all()
        )

        # Recent leads
        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(20).all()

        # Build HTML
        hot_html = ""
        for l in hot:
            hot_html += f"""
            <div class="card hot">
              <div class="row">
                <div><b>{l.full_name or "Unknown"}</b> <span class="muted">[{l.state}]</span></div>
                <div><a class="btn" href="/leads/{l.id}">Open</a></div>
              </div>
              <div class="muted">📞 {l.phone} &nbsp; ✉️ {l.email or "—"}</div>
            </div>
            """

        leads_html = ""
        for l in leads:
            leads_html += f"""
            <div class="card">
              <div class="row">
                <div>
                  <b>{l.full_name or "Unknown"}</b>
                  <span class="pill">{l.state}</span>
                </div>
                <div class="row" style="gap:8px;">
                  <a class="btn" href="/leads/{l.id}">Open</a>
                  <form method="post" action="/leads/delete/{l.id}" style="margin:0;">
                    <button class="btn danger" type="submit">Delete</button>
                  </form>
                </div>
              </div>
              <div class="muted">📞 {l.phone or "—"} &nbsp; ✉️ {l.email or "—"}</div>
            </div>
            """

        logs_html = ""
        for x in recent_logs:
            logs_html += f"""
            <div class="log">
              <b>{x.event}</b> <span class="muted">lead={x.lead_id} • {x.created_at}</span>
              <div class="muted" style="white-space:pre-wrap">{(x.detail or "")[:400]}</div>
            </div>
            """

        return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AgencyVault Command Center</title>
  <style>
    body {{
      background:#0b0f17; color:#e6edf3; font-family:system-ui;
      padding:20px; max-width:1100px; margin:0 auto;
    }}
    a {{ color:#8ab4f8; text-decoration:none; }}
    .topbar {{ display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }}
    .title {{ font-size:28px; font-weight:800; letter-spacing:0.2px; }}
    .nav {{ display:flex; gap:10px; flex-wrap:wrap; }}
    .btn {{
      background:#111827; border:1px solid #223047; color:#e6edf3;
      padding:8px 12px; border-radius:10px; cursor:pointer; display:inline-block;
    }}
    .btn:hover {{ border-color:#3a557d; }}
    .danger {{ background:#2a0f14; border-color:#5b1a22; }}
    .grid {{ display:grid; grid-template-columns:repeat(12,1fr); gap:12px; margin-top:14px; }}
    .panel {{
      background:#0f1624; border:1px solid #1f2b3e; border-radius:16px; padding:14px;
    }}
    .muted {{ opacity:0.75; font-size:13px; }}
    .kpis {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }}
    .kpi {{ background:#0b1220; border:1px solid #1f2b3e; border-radius:14px; padding:12px; }}
    .kpi b {{ font-size:20px; }}
    .card {{
      background:#0b1220; border:1px solid #1f2b3e; border-radius:14px; padding:12px; margin-top:10px;
    }}
    .card.hot {{ border-color:#5b4b14; background:#141003; }}
    .row {{ display:flex; justify-content:space-between; align-items:center; gap:10px; }}
    .pill {{ margin-left:8px; padding:2px 8px; border-radius:999px; background:#111827; border:1px solid #223047; font-size:12px; opacity:0.9; }}
    .log {{ padding:10px 0; border-bottom:1px solid #1f2b3e; }}
    .chatbox {{
      background:#0b1220; border:1px solid #1f2b3e; border-radius:14px; padding:12px;
    }}
    textarea {{
      width:100%; background:#0b0f17; color:#e6edf3; border:1px solid #223047; border-radius:12px;
      padding:10px; min-height:92px; font-size:14px;
    }}
    .small {{ font-size:12px; opacity:0.8; }}
    .col-7 {{ grid-column: span 7; }}
    .col-5 {{ grid-column: span 5; }}
    .col-12 {{ grid-column: span 12; }}
    @media (max-width: 900px) {{
      .col-7,.col-5 {{ grid-column: span 12; }}
      .kpis {{ grid-template-columns:repeat(2,1fr); }}
    }}
  </style>
</head>
<body>

  <div class="topbar">
    <div class="title">AgencyVault — Command Center</div>
    <div class="nav">
      <a class="btn" href="/leads/new">➕ New Lead</a>
      <a class="btn" href="/imports">⬆️ Imports</a>
      <a class="btn" href="/actions">✅ Action Queue</a>
      <a class="btn" href="/activity">🧾 Activity</a>
      <a class="btn" href="/ai/plan">🤖 Run AI Planner</a>
    </div>
  </div>

  <div class="grid">
    <div class="panel col-12">
      <div class="kpis">
        <div class="kpi"><div class="muted">Total Leads</div><b>{total_leads}</b></div>
        <div class="kpi"><div class="muted">NEW</div><b>{new_leads}</b></div>
        <div class="kpi"><div class="muted">WORKING</div><b>{working}</b></div>
        <div class="kpi"><div class="muted">Pending Actions</div><b>{pending_actions}</b></div>
      </div>
      <div class="muted" style="margin-top:10px;">Compliance: DO_NOT_CONTACT = {dnc}</div>
    </div>

    <div class="panel col-7">
      <div class="row">
        <div><b>🔥 Hot Leads (replied in last 24h)</b></div>
        <div class="muted">AI escalates these to you automatically</div>
      </div>
      {hot_html or '<div class="muted" style="margin-top:10px;">No hot replies yet.</div>'}
    </div>

    <div class="panel col-5">
      <div class="row">
        <div><b>💬 AI Employee (Control Chat)</b></div>
        <div class="muted">You can command the system here</div>
      </div>
      <div class="chatbox" style="margin-top:10px;">
        <textarea id="msg" placeholder="Try: 'show hot leads', 'run planner', 'why is lead 12 stuck', 'pause outreach'"></textarea>
        <div class="row" style="margin-top:10px;">
          <button class="btn" onclick="sendMsg()">Send</button>
          <div class="small muted">Everything is logged. Safe + auditable.</div>
        </div>
        <pre id="out" class="muted" style="white-space:pre-wrap;margin-top:10px;"></pre>
      </div>
    </div>

    <div class="panel col-7">
      <div class="row">
        <div><b>🧾 Live Activity</b></div>
        <div class="muted">What AI is doing right now</div>
      </div>
      <div style="margin-top:8px;">{logs_html or '<div class="muted">No activity yet.</div>'}</div>
    </div>

    <div class="panel col-5">
      <div class="row">
        <div><b>📇 Recent Leads</b></div>
        <div class="muted">Sorted newest first</div>
      </div>
      {leads_html or '<div class="muted" style="margin-top:10px;">No leads yet.</div>'}
    </div>
  </div>

<script>
async function sendMsg() {{
  const msg = document.getElementById("msg").value;
  const out = document.getElementById("out");
  out.textContent = "Thinking...";
  try {{
    const r = await fetch("/api/assistant", {{
      method:"POST",
      headers:{{"Content-Type":"application/json"}},
      body:JSON.stringify({{message:msg}})
    }});
    const data = await r.json();
    out.textContent = data.reply || JSON.stringify(data, null, 2);
  }} catch (e) {{
    out.textContent = "Error: " + e;
  }}
}}
</script>

</body>
</html>
        """)
    finally:
        db.close()

# ---------------------------
# Assistant API (enterprise control interface)
# ---------------------------
@app.post("/api/assistant")
async def assistant_api(payload: dict):
    msg = (payload.get("message") or "").strip().lower()
    db = SessionLocal()
    try:
        _log(db, None, None, "ASSISTANT_COMMAND", msg)
        db.commit()

        if not msg:
            return {"reply": "Type a command like: 'run planner', 'show hot leads', 'show pending actions'."}

        if "run" in msg and "plan" in msg:
            out = plan_actions(db, batch_size=25)
            _log(db, None, out.get("run_id"), "ASSISTANT_RESULT", f"Planner ran: {out}")
            db.commit()
            return {"reply": f"✅ Planner ran.\nPlanned actions: {out['planned_actions']}\nConsidered: {out['considered']}"}

        if "hot" in msg:
            since = _now() - timedelta(hours=24)
            hot = (
                db.query(Lead)
                .join(Message, Message.lead_id == Lead.id)
                .filter(Message.direction == "IN", Message.created_at >= since, Lead.state != "DO_NOT_CONTACT")
                .order_by(Message.created_at.desc())
                .limit(10)
                .all()
            )
            lines = [f"- #{l.id} {l.full_name} {l.phone} [{l.state}]" for l in hot]
            return {"reply": "🔥 Hot leads:\n" + ("\n".join(lines) if lines else "None in last 24h.")}

        if "pending" in msg and "action" in msg:
            actions = (
                db.query(Action)
                .filter(Action.status == "PENDING")
                .order_by(Action.created_at.asc())
                .limit(25)
                .all()
            )
            lines = [f"- Action #{a.id} {a.type} lead={a.lead_id}" for a in actions]
            return {"reply": "✅ Pending actions:\n" + ("\n".join(lines) if lines else "None.")}

        if "lead" in msg and any(ch.isdigit() for ch in msg):
            # quick lead lookup: "lead 12"
            nums = re.findall(r"\d+", msg)
            lead_id = int(nums[0]) if nums else 0
            lead = db.query(Lead).filter_by(id=lead_id).first()
            if not lead:
                return {"reply": f"No lead found with id {lead_id}."}
            last = lead.last_contacted_at or "—"
            return {"reply": f"Lead #{lead.id}: {lead.full_name}\nState: {lead.state}\nPhone: {lead.phone}\nEmail: {lead.email or '—'}\nLast contacted: {last}"}

        if "pause" in msg:
            # simple global pause flag stored in DB via LeadMemory with lead_id=0 is not valid in schema.
            # We store pause in environment later; for now we tell you.
            return {"reply": "To pause outreach, set ENABLE_AUTORUN=0 in Render (worker will stop sending)."}

        return {"reply": "Commands I understand:\n- run planner\n- show hot leads\n- show pending actions\n- lead <id>\n- pause outreach\n\nTry: 'run planner'."}
    finally:
        db.close()

# ---------------------------
# Leads CRUD
# ---------------------------
@app.get("/leads/new", response_class=HTMLResponse)
def leads_new_form():
    return HTMLResponse("""
    <html><body style="font-family:system-ui;padding:20px;background:#0b0f17;color:#e6edf3">
      <h2>New Lead</h2>
      <form method="post" action="/leads/new">
        <div>Name<br><input name="full_name" style="width:320px"/></div><br>
        <div>Phone<br><input name="phone" style="width:320px"/></div><br>
        <div>Email<br><input name="email" style="width:320px"/></div><br>
        <button type="submit">Create</button>
      </form>
      <p><a href="/dashboard">Back</a></p>
    </body></html>
    """)

@app.post("/leads/new")
def leads_new(full_name: str = Form(""), phone: str = Form(""), email: str = Form("")):
    db = SessionLocal()
    try:
        p = normalize_phone(phone)
        e = clean_text(email)
        n = clean_text(full_name) or "Unknown"
        if not p:
            return JSONResponse({"error": "invalid phone"}, status_code=400)
        if dedupe_exists(db, p, e):
            return JSONResponse({"error": "lead exists"}, status_code=409)
        db.add(Lead(full_name=n, phone=p, email=e, state="NEW", created_at=_now(), updated_at=_now()))
        _log(db, None, None, "LEAD_CREATED", f"{n} {p}")
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()

@app.post("/leads/delete/{lead_id}")
def leads_delete(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if lead:
            _log(db, lead.id, None, "LEAD_DELETED", f"{lead.full_name} {lead.phone}")
            db.delete(lead)
            db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()

@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return HTMLResponse("Not found", status_code=404)

        actions = db.query(Action).filter(Action.lead_id == lead_id).order_by(Action.created_at.desc()).limit(100).all()
        logs = db.query(AuditLog).filter(AuditLog.lead_id == lead_id).order_by(AuditLog.created_at.desc()).limit(200).all()
        msgs = db.query(Message).filter(Message.lead_id == lead_id).order_by(Message.created_at.desc()).limit(50).all()

        action_html = ""
        for a in actions:
            action_html += f"""
            <div style="padding:8px 0;border-bottom:1px solid #222">
              <b>{a.type}</b> [{a.status}] tool={a.tool}<br>
              <div style="opacity:0.9;white-space:pre-wrap">{a.payload_json}</div>
              <div style="opacity:0.8;color:#ffb4b4">{a.error or ""}</div>
            </div>
            """

        msg_html = ""
        for m in msgs:
            msg_html += f"""
            <div style="padding:8px 0;border-bottom:1px solid #222">
              <b>{m.direction}</b> {m.channel} <span style="opacity:0.7">{m.created_at}</span><br>
              <div style="white-space:pre-wrap;opacity:0.9">{m.body}</div>
            </div>
            """

        log_html = ""
        for l in logs:
            # Show recording URLs clickable if present
            detail = l.detail or ""
            if "RecordingUrl=" in detail or "url=" in detail:
                detail = detail.replace("url=", "url=")
            log_html += f"""
            <div style="padding:8px 0;border-bottom:1px solid #222">
              <b>{l.event}</b> <span style="opacity:0.7">{l.created_at}</span><br>
              <div style="white-space:pre-wrap;opacity:0.9">{detail}</div>
            </div>
            """

        return HTMLResponse(f"""
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1" /></head>
        <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
          <a href="/dashboard">← Back</a>
          <h2 style="margin:10px 0 6px 0;">{lead.full_name or "Unknown"}</h2>
          <div>📞 {lead.phone or "—"}</div>
          <div>✉️ {lead.email or "—"}</div>
          <div style="opacity:0.8">State: {lead.state}</div>

          <h3 style="margin-top:18px;">Messages</h3>
          <div style="background:#0f1624;padding:12px;border-radius:10px">{msg_html or "<div>No messages</div>"}</div>

          <h3 style="margin-top:18px;">Actions</h3>
          <div style="background:#0f1624;padding:12px;border-radius:10px">{action_html or "<div>No actions</div>"}</div>

          <h3 style="margin-top:18px;">Activity (Audit Log)</h3>
          <div style="background:#0f1624;padding:12px;border-radius:10px">{log_html or "<div>No activity</div>"}</div>
        </body></html>
        """)
    finally:
        db.close()

# ---------------------------
# Lists
# ---------------------------
@app.get("/activity", response_class=HTMLResponse)
def activity():
    db = SessionLocal()
    try:
        logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(400).all()
        rows = ""
        for l in logs:
            rows += f"<div style='padding:8px 0;border-bottom:1px solid #222'><b>{l.event}</b> lead={l.lead_id} run={l.run_id} <span style='opacity:0.7'>{l.created_at}</span><div style='white-space:pre-wrap;opacity:0.9'>{l.detail}</div></div>"
        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
        <a href="/dashboard">← Back</a>
        <h2>Activity</h2>
        <div style="background:#0f1624;padding:12px;border-radius:10px">{rows or "No activity"}</div>
        </body></html>
        """)
    finally:
        db.close()

@app.get("/actions", response_class=HTMLResponse)
def actions_page():
    db = SessionLocal()
    try:
        actions = db.query(Action).order_by(Action.created_at.desc()).limit(400).all()
        rows = ""
        for a in actions:
            rows += f"<div style='padding:8px 0;border-bottom:1px solid #222'>#{a.id} <b>{a.type}</b> lead={a.lead_id} [{a.status}] <span style='opacity:0.7'>{a.created_at}</span></div>"
        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
        <a href="/dashboard">← Back</a>
        <h2>Action Queue</h2>
        <div style="background:#0f1624;padding:12px;border-radius:10px">{rows or "No actions"}</div>
        </body></html>
        """)
    finally:
        db.close()

# ---------------------------
# AI Planner trigger
# ---------------------------
@app.get("/ai/plan")
def ai_plan():
    db = SessionLocal()
    try:
        out = plan_actions(db, batch_size=25)
        _log(db, None, out.get("run_id"), "AI_PLAN_TRIGGERED", f"{out}")
        db.commit()
        return out
    finally:
        db.close()

# ---------------------------
# Imports
# ---------------------------
@app.get("/imports", response_class=HTMLResponse)
def imports_page():
    return HTMLResponse("""
    <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:900px;margin:0 auto;">
      <a href="/dashboard">← Back</a>
      <h2>Imports</h2>

      <h3>CSV Upload</h3>
      <form action="/import/csv" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept=".csv" />
        <button type="submit">Upload</button>
      </form>

      <h3 style="margin-top:18px;">Image Upload (JPEG/PNG)</h3>
      <form action="/import/image" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept="image/*" />
        <button type="submit">Upload</button>
      </form>

      <h3 style="margin-top:18px;">Google Sheet</h3>
      <form action="/import/google-sheet" method="post">
        <div>Service Account JSON<br><textarea name="creds_json" style="width:100%;height:120px"></textarea></div>
        <div>Spreadsheet ID<br><input name="spreadsheet_id" style="width:100%"/></div>
        <div>Range (example: Sheet1!A1:Z)<br><input name="range_name" style="width:100%"/></div>
        <button type="submit">Import</button>
      </form>

      <h3 style="margin-top:18px;">Google Drive CSV</h3>
      <form action="/import/drive-csv" method="post">
        <div>Service Account JSON<br><textarea name="creds_json" style="width:100%;height:120px"></textarea></div>
        <div>File ID<br><input name="file_id" style="width:100%"/></div>
        <button type="submit">Import</button>
      </form>

      <h3 style="margin-top:18px;">Google Doc</h3>
      <form action="/import/google-doc" method="post">
        <div>Service Account JSON<br><textarea name="creds_json" style="width:100%;height:120px"></textarea></div>
        <div>Doc File ID<br><input name="file_id" style="width:100%"/></div>
        <button type="submit">Import</button>
      </form>
    </body></html>
    """)

def _import_row(db: Session, row: dict) -> bool:
    phone = normalize_phone(row.get("phone") or row.get("Phone") or row.get("mobile") or row.get("Mobile"))
    email = clean_text(row.get("email") or row.get("Email"))
    name = clean_text(row.get("name") or row.get("full_name") or row.get("Name") or row.get("Full Name")) or "Unknown"

    if not phone:
        return False
    if dedupe_exists(db, phone, email):
        return False

    db.add(Lead(full_name=name, phone=phone, email=email, state="NEW", created_at=_now(), updated_at=_now()))
    return True

@app.post("/import/csv")
async def import_csv(file: UploadFile = File(...)):
    db = SessionLocal()
    try:
        raw = (await file.read()).decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(raw))
        added = 0
        for row in reader:
            if _import_row(db, row):
                added += 1
        _log(db, None, None, "IMPORT_CSV", f"imported={added}")
        db.commit()
        return {"imported": added}
    finally:
        db.close()

@app.post("/import/image")
async def import_image(file: UploadFile = File(...)):
    db = SessionLocal()
    try:
        data = await file.read()
        text_data = extract_text_from_image_bytes(data)
        leads = parse_leads_from_text(text_data)
        added = 0
        for l in leads:
            if _import_row(db, l):
                added += 1
        _log(db, None, None, "IMPORT_IMAGE", f"imported={added}")
        db.commit()
        return {"imported": added}
    finally:
        db.close()

@app.post("/import/google-sheet")
def import_sheet(creds_json: str = Form(...), spreadsheet_id: str = Form(...), range_name: str = Form(...)):
    db = SessionLocal()
    try:
        creds = json.loads(creds_json)
        rows = import_google_sheet(creds, spreadsheet_id, range_name)
        added = 0
        for row in rows:
            if _import_row(db, row):
                added += 1
        _log(db, None, None, "IMPORT_SHEET", f"imported={added}")
        db.commit()
        return {"imported": added}
    finally:
        db.close()

@app.post("/import/drive-csv")
def import_drive(creds_json: str = Form(...), file_id: str = Form(...)):
    db = SessionLocal()
    try:
        creds = json.loads(creds_json)
        rows = import_drive_csv(creds, file_id)
        added = 0
        for row in rows:
            if _import_row(db, row):
                added += 1
        _log(db, None, None, "IMPORT_DRIVE_CSV", f"imported={added}")
        db.commit()
        return {"imported": added}
    finally:
        db.close()

@app.post("/import/google-doc")
def import_doc(creds_json: str = Form(...), file_id: str = Form(...)):
    db = SessionLocal()
    try:
        creds = json.loads(creds_json)
        text_data = import_google_doc_text(creds, file_id)
        leads = parse_leads_from_text(text_data)
        added = 0
        for l in leads:
            if _import_row(db, l):
                added += 1
        _log(db, None, None, "IMPORT_GOOGLE_DOC", f"imported={added}")
        db.commit()
        return {"imported": added}
    finally:
        db.close()

# ---------------------------
# Twilio: Voice TwiML
# ---------------------------
@app.get("/twilio/voice/twiml")
@app.post("/twilio/voice/twiml")
def twilio_twiml(lead_id: int | None = None):
    db = SessionLocal()
    try:
        name = "there"
        if lead_id:
            lead = db.query(Lead).filter_by(id=lead_id).first()
            if lead and lead.full_name:
                parts = lead.full_name.split()
                if parts and parts[0]:
                    name = parts[0]

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="alice">Hi {name}. This is Nick’s office calling about the life insurance information you requested.</Say>
  <Pause length="1"/>
  <Say voice="alice">If now is not a good time, you can text us back and we will follow up.</Say>
</Response>
"""
        return Response(content=twiml, media_type="text/xml")
    finally:
        db.close()

# ---------------------------
# Twilio: Call Status
# ---------------------------
@app.post("/twilio/call/status")
def twilio_call_status(CallSid: str = Form(...), CallStatus: str = Form(...)):
    db = SessionLocal()
    try:
        lead_id = lead_id_for_call_sid(CallSid)
        _log(db, lead_id, None, "CALL_STATUS", f"sid={CallSid} status={CallStatus}")
        db.commit()
        return {"ok": True}
    finally:
        db.close()

# ---------------------------
# Twilio: Recording
# ---------------------------
@app.post("/twilio/recording")
def twilio_recording(RecordingSid: str = Form(...), RecordingUrl: str = Form(...), CallSid: str = Form(...)):
    db = SessionLocal()
    try:
        lead_id = lead_id_for_call_sid(CallSid)
        # Twilio RecordingUrl usually needs extension to play
        playable = (RecordingUrl or "").strip()
        mp3 = playable + ".mp3" if playable and not playable.endswith(".mp3") else playable
        _log(db, lead_id, None, "CALL_RECORDING", f"sid={CallSid} recordingSid={RecordingSid} url={playable} mp3={mp3}")
        db.commit()
        return {"ok": True}
    finally:
        db.close()

# ---------------------------
# Twilio: Inbound SMS (enterprise money-maker)
# ---------------------------
@app.post("/twilio/sms/inbound")
def twilio_sms_inbound(
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form("")
):
    db = SessionLocal()
    try:
        from_phone = normalize_phone(From) or From
        to_phone = normalize_phone(To) or To
        body = (Body or "").strip()

        lead = db.query(Lead).filter(Lead.phone == from_phone).first()
        if not lead:
            # Unknown inbound message: log only
            _log(db, None, None, "SMS_IN_UNKNOWN", f"From={from_phone} Body={body}")
            db.commit()
            return Response(content="<Response></Response>", media_type="text/xml")

        db.add(Message(
            lead_id=lead.id,
            direction="IN",
            channel="SMS",
            from_number=from_phone,
            to_number=to_phone,
            body=body,
            provider_sid=MessageSid or "",
        ))

        _log(db, lead.id, None, "SMS_IN", body)

        low = body.lower()

        # Compliance: STOP / DNC
        if any(x in low for x in ["stop", "unsubscribe", "do not contact", "dont contact", "dnc"]):
            lead.state = "DO_NOT_CONTACT"
            lead.updated_at = _now()
            cancel_pending_actions(db, lead.id, "Inbound STOP/DNC")
            _log(db, lead.id, None, "COMPLIANCE_DNC", "Lead opted out via inbound SMS")
            db.commit()
            return Response(content="<Response></Response>", media_type="text/xml")

        # Hot intent / escalation triggers
        if any(x in low for x in ["call me", "ready", "yes", "now", "interested", "today"]):
            lead.state = "CONTACTED"
            lead.updated_at = _now()
            _log(db, lead.id, None, "HOT_LEAD_DETECTED", body)
            try:
                send_alert_sms(f"🔥 HOT LEAD: {lead.full_name} {lead.phone} replied: {body}")
            except Exception as e:
                _log(db, lead.id, None, "ALERT_ERROR", str(e))
            db.commit()
            return Response(content="<Response></Response>", media_type="text/xml")

        # Otherwise keep them WORKING and let planner continue
        if lead.state == "NEW":
            lead.state = "WORKING"
        lead.updated_at = _now()
        db.commit()
        return Response(content="<Response></Response>", media_type="text/xml")
    finally:
        db.close()
# ============================================================
# ADMIN — MASS DELETE (SAFE, EXPLICIT)
# ============================================================

@app.get("/admin/delete-all-leads", response_class=HTMLResponse)
def admin_delete_all_leads_page():
    return HTMLResponse("""
    <html>
      <body style="font-family:system-ui;background:#0b0f17;color:#e6edf3;padding:40px">
        <h2>⚠️ Delete ALL Leads</h2>
        <p>This will permanently delete:</p>
        <ul>
          <li>All leads</li>
          <li>All AI actions</li>
          <li>All AI memory</li>
        </ul>

        <form method="post" action="/admin/delete-all-leads">
          <p>Type <b>DELETE ALL LEADS</b> to confirm:</p>
          <input name="confirm" style="padding:8px;width:300px" />
          <br><br>
          <button style="background:#b91c1c;color:white;padding:10px 18px">
            Permanently Delete Everything
          </button>
        </form>
      </body>
    </html>
    """)


@app.post("/admin/delete-all-leads")
def admin_delete_all_leads(confirm: str = Form(...)):
    if confirm.strip() != "DELETE ALL LEADS":
        return {"error": "Confirmation text does not match"}

    db = SessionLocal()
    try:
        # Order matters because of foreign keys
        db.execute(text("DELETE FROM actions"))
        db.execute(text("DELETE FROM lead_memory"))
        db.execute(text("DELETE FROM leads"))
        db.commit()
        return {"ok": True, "message": "All leads and related data deleted"}
    finally:
        db.close()
