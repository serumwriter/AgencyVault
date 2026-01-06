import csv
import io
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import httpx
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from .database import engine, SessionLocal
from .models import Base, Lead, Action, AgentRun, LeadMemory, AuditLog, Message
from .google_drive_import import import_google_sheet, import_drive_csv, import_google_doc_text
from .image_import import extract_text_from_image_bytes, parse_leads_from_text
from .twilio_client import (
    send_alert_sms,
    send_lead_sms,
    make_call,
)

app = FastAPI(title="AgencyVault — Command Center")

# =========================
# Startup / Schema
# =========================
@app.on_event("startup")
def _startup():
    Base.metadata.create_all(bind=engine)

# =========================
# Config
# =========================
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
BAD_NAME_WORDS = {
    "lead", "bronze", "silver", "gold", "ethos",
    "facebook", "insurance", "prospect", "unknown", "test",
    "meta", "client", "customer", "applicant"
}

def _now():
    return datetime.utcnow()

def clean_text(val):
    if val is None:
        return None
    return CONTROL_RE.sub("", str(val)).replace("\x00", "").strip() or None

def normalize_phone(val):
    val = clean_text(val) or ""
    d = re.sub(r"\D", "", val)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return None

def safe_first_name(full_name: str) -> str:
    if not full_name:
        return ""
    first = full_name.strip().split()[0].lower()
    if first in BAD_NAME_WORDS or len(first) < 2 or any(c.isdigit() for c in first):
        return ""
    return first.capitalize()

def dedupe_exists(db: Session, phone: Optional[str], email: Optional[str]) -> bool:
    if phone and db.query(Lead).filter(Lead.phone == phone).first():
        return True
    if email and db.query(Lead).filter(Lead.email == email).first():
        return True
    return False

def _log(db: Session, lead_id: Optional[int], run_id: Optional[int], event: str, detail: str):
    db.add(AuditLog(
        lead_id=lead_id,
        run_id=run_id,
        event=event,
        detail=(detail or "")[:5000],
        created_at=_now(),
    ))

def mem_set(db: Session, lead_id: int, key: str, value: str):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    if row:
        row.value = value
        row.updated_at = _now()
    else:
        db.add(LeadMemory(lead_id=lead_id, key=key, value=value, updated_at=_now()))

def mem_get(db: Session, lead_id: int, key: str) -> Optional[str]:
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    return row.value if row else None

def cancel_pending_actions(db: Session, lead_id: int, reason: str):
    q = db.query(Action).filter(Action.lead_id == lead_id, Action.status == "PENDING").all()
    for a in q:
        a.status = "SKIPPED"
        a.error = f"Canceled: {reason}"
        a.finished_at = _now()
    _log(db, lead_id, None, "ACTIONS_CANCELED", reason)

def require_admin(req: Request):
    token = req.headers.get("x-admin-token", "") or req.query_params.get("token", "")
    if not token or token.strip() != os.getenv("ADMIN_TOKEN", ""):
        return False
    return True

# =========================
# OpenAI Chat (server-side)
# =========================
async def llm_reply(user_text: str, context: str = "") -> str:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return "OPENAI_API_KEY is missing in Render Environment."

    model = (os.getenv("OPENAI_MODEL") or "gpt-5").strip()
    system = (
        "You are AgencyVault AI Employee for a life insurance agency. "
        "Be direct, practical, and sales-focused. "
        "Never hallucinate database values; if unsure, say what you can check next. "
        "When asked for actions, output step-by-step operator instructions."
    )
    if context:
        system += "\n\nContext:\n" + context

    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
    }

    # Use Responses API over raw completions-style
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code >= 400:
            return f"AI error: {r.status_code} {r.text[:500]}"
        data = r.json()

    # Try to extract text safely
    try:
        # Responses API returns output array; find first text item
        out = data.get("output", [])
        chunks = []
        for item in out:
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    chunks.append(c.get("text", ""))
        text_out = "\n".join([x for x in chunks if x]).strip()
        return text_out or "AI returned no text."
    except Exception:
        return "AI response parsing failed."

# =========================
# Health / Root
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
# Dashboard UI (Control Room)
# =========================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    try:
        total_leads = db.query(Lead).count()
        new_leads = db.query(Lead).filter(Lead.state == "NEW").count()
        working = db.query(Lead).filter(Lead.state == "WORKING").count()
        contacted = db.query(Lead).filter(Lead.state == "CONTACTED").count()
        dnc = db.query(Lead).filter(Lead.state == "DO_NOT_CONTACT").count()

        pending_actions = db.query(Action).filter(Action.status == "PENDING").count()
        failed_actions = db.query(Action).filter(Action.status == "FAILED").count()

        recent_logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(12).all()

        since = _now() - timedelta(hours=24)
        hot = (
            db.query(Lead)
            .join(Message, Message.lead_id == Lead.id)
            .filter(Message.direction == "IN", Message.created_at >= since, Lead.state != "DO_NOT_CONTACT")
            .order_by(Message.created_at.desc())
            .limit(8)
            .all()
        )

        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(20).all()

        hot_html = ""
        for l in hot:
            hot_html += f"""
            <div class="card hot">
              <div class="row">
                <div><b>#{l.id} {l.full_name or "Unknown"}</b> <span class="muted">[{l.state}]</span></div>
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
                  <b>#{l.id} {l.full_name or "Unknown"}</b>
                  <span class="pill">{l.state}</span>
                </div>
                <div class="row" style="gap:8px;">
                  <a class="btn" href="/leads/{l.id}">Open</a>
                  <form method="post" action="/leads/delete/{l.id}" style="margin:0;"
                        onsubmit="return confirm('Delete this lead permanently?');">
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
              <div class="muted" style="white-space:pre-wrap">{(x.detail or "")[:500]}</div>
            </div>
            """

        return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AgencyVault — Command Center</title>
  <style>
    body {{
      background: radial-gradient(1200px 700px at 20% 0%, #13203a 0%, #0b0f17 55%, #070a10 100%);
      color:#e6edf3; font-family:system-ui;
      padding:20px; max-width:1200px; margin:0 auto;
    }}
    a {{ color:#8ab4f8; text-decoration:none; }}
    .topbar {{ display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }}
    .title {{ font-size:28px; font-weight:900; letter-spacing:0.3px; }}
    .subtitle {{ opacity:0.75; font-size:13px; margin-top:4px; }}
    .nav {{ display:flex; gap:10px; flex-wrap:wrap; }}
    .btn {{
      background: linear-gradient(180deg, #121b2d 0%, #0c1322 100%);
      border:1px solid #223047; color:#e6edf3;
      padding:9px 12px; border-radius:12px; cursor:pointer; display:inline-block;
      box-shadow: 0 10px 20px rgba(0,0,0,0.25);
    }}
    .btn:hover {{ border-color:#3a557d; transform: translateY(-1px); }}
    .danger {{ background: linear-gradient(180deg, #2a0f14 0%, #17070b 100%); border-color:#5b1a22; }}
    .grid {{ display:grid; grid-template-columns:repeat(12,1fr); gap:12px; margin-top:14px; }}
    .panel {{
      background: rgba(12,19,34,0.75);
      border:1px solid rgba(50,74,110,0.35);
      border-radius:18px; padding:14px;
      backdrop-filter: blur(8px);
      box-shadow: 0 18px 40px rgba(0,0,0,0.35);
    }}
    .muted {{ opacity:0.75; font-size:13px; }}
    .kpis {{ display:grid; grid-template-columns:repeat(6,1fr); gap:10px; }}
    .kpi {{
      background: rgba(8,12,20,0.75);
      border:1px solid rgba(50,74,110,0.35);
      border-radius:16px; padding:12px;
    }}
    .kpi b {{ font-size:20px; }}
    .card {{
      background: rgba(8,12,20,0.75);
      border:1px solid rgba(50,74,110,0.35);
      border-radius:16px; padding:12px; margin-top:10px;
    }}
    .card.hot {{ border-color: rgba(255,214,102,0.35); box-shadow: 0 0 0 1px rgba(255,214,102,0.15) inset; }}
    .row {{ display:flex; justify-content:space-between; align-items:center; gap:10px; }}
    .pill {{
      margin-left:8px; padding:2px 10px; border-radius:999px;
      background: rgba(18,27,45,0.8);
      border:1px solid rgba(50,74,110,0.35);
      font-size:12px; opacity:0.95;
    }}
    .log {{ padding:10px 0; border-bottom:1px solid rgba(50,74,110,0.25); }}
    textarea {{
      width:100%; background: rgba(8,12,20,0.7); color:#e6edf3;
      border:1px solid rgba(50,74,110,0.35); border-radius:14px;
      padding:10px; min-height:100px; font-size:14px;
    }}
    pre {{
      background: rgba(8,12,20,0.7);
      border:1px solid rgba(50,74,110,0.25);
      border-radius:14px; padding:10px;
    }}
    .col-7 {{ grid-column: span 7; }}
    .col-5 {{ grid-column: span 5; }}
    .col-12 {{ grid-column: span 12; }}
    @media (max-width: 980px) {{
      .col-7,.col-5 {{ grid-column: span 12; }}
      .kpis {{ grid-template-columns:repeat(2,1fr); }}
    }}
  </style>
</head>
<body>

  <div class="topbar">
    <div>
      <div class="title">AgencyVault — Command Center</div>
      <div class="subtitle">Live ops • AI employee • Outreach control • Compliance-safe</div>
    </div>
    <div class="nav">
      <a class="btn" href="/imports">⬆️ Imports</a>
      <a class="btn" href="/actions">✅ Action Queue</a>
      <a class="btn" href="/activity">🧾 Activity</a>
      <a class="btn" href="/ai/plan">🤖 Run AI Planner</a>
      <a class="btn" href="/admin">🛡️ Admin</a>
    </div>
  </div>

  <div class="grid">
    <div class="panel col-12">
      <div class="kpis">
        <div class="kpi"><div class="muted">Total</div><b>{total_leads}</b></div>
        <div class="kpi"><div class="muted">NEW</div><b>{new_leads}</b></div>
        <div class="kpi"><div class="muted">WORKING</div><b>{working}</b></div>
        <div class="kpi"><div class="muted">CONTACTED</div><b>{contacted}</b></div>
        <div class="kpi"><div class="muted">PENDING Actions</div><b>{pending_actions}</b></div>
        <div class="kpi"><div class="muted">FAILED Actions</div><b>{failed_actions}</b></div>
      </div>
      <div class="muted" style="margin-top:10px;">Compliance: DO_NOT_CONTACT = {dnc}</div>
    </div>

    <div class="panel col-7">
      <div class="row">
        <div><b>🔥 Hot Replies (last 24h)</b></div>
        <div class="muted">These are high intent. Call them first.</div>
      </div>
      {hot_html or '<div class="muted" style="margin-top:10px;">No hot replies yet.</div>'}
    </div>

    <div class="panel col-5">
      <div class="row">
        <div><b>💬 AI Employee</b></div>
        <div class="muted">Ask anything, run actions, troubleshoot.</div>
      </div>
      <div style="margin-top:10px;">
        <textarea id="msg" placeholder="Try: 'What should I do today?' or 'Why are actions failing?' or 'Write a script for aged leads that opted in last year.'"></textarea>
        <div class="row" style="margin-top:10px;">
          <button class="btn" onclick="sendMsg()">Send</button>
          <div class="muted" style="font-size:12px;">Server-side AI • Logged • Safe</div>
        </div>
        <pre id="out" class="muted" style="white-space:pre-wrap;margin-top:10px;"></pre>
      </div>
    </div>

    <div class="panel col-7">
      <div class="row">
        <div><b>🧾 Live Activity</b></div>
        <div class="muted">What the system is doing</div>
      </div>
      <div style="margin-top:8px;">{logs_html or '<div class="muted">No activity yet.</div>'}</div>
    </div>

    <div class="panel col-5">
      <div class="row">
        <div><b>📇 Recent Leads</b></div>
        <div class="muted">Newest first • quick actions</div>
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

# =========================
# Assistant API (real AI)
# =========================
@app.post("/api/assistant")
async def assistant_api(payload: dict):
    msg = (payload.get("message") or "").strip()
    db = SessionLocal()
    try:
        _log(db, None, None, "ASSISTANT_COMMAND", msg)
        db.commit()

        if not msg:
            return {"reply": "Type something like: 'What should I do today?' or 'Run planner for 25 leads'."}

        # lightweight command shortcuts
        low = msg.lower()
        if "run" in low and "planner" in low:
            out = plan_actions(db, batch_size=int(os.getenv("AI_BATCH_SIZE", "25")))
            _log(db, None, out.get("run_id"), "ASSISTANT_RESULT", f"Planner ran: {out}")
            db.commit()
            return {"reply": f"✅ Planner ran.\nPlanned: {out['planned_actions']}\nConsidered: {out['considered']}\nRun ID: {out['run_id']}"}

        # give AI some live context (counts + last failures)
        failed = (
            db.query(Action)
            .filter(Action.status == "FAILED")
            .order_by(Action.finished_at.desc().nullslast(), Action.id.desc())
            .limit(5)
            .all()
        )
        failed_lines = []
        for a in failed:
            failed_lines.append(f"Action#{a.id} {a.type} lead={a.lead_id} err={a.error[:120]}")

        context = (
            f"Counts: total_leads={db.query(Lead).count()}, "
            f"new={db.query(Lead).filter(Lead.state=='NEW').count()}, "
            f"pending_actions={db.query(Action).filter(Action.status=='PENDING').count()}, "
            f"failed_actions={db.query(Action).filter(Action.status=='FAILED').count()}.\n"
            f"Recent failures:\n" + ("\n".join(failed_lines) if failed_lines else "None.")
        )

        reply = await llm_reply(msg, context=context)
        _log(db, None, None, "ASSISTANT_REPLY", reply[:2000])
        db.commit()
        return {"reply": reply}
    finally:
        db.close()

# =========================
# AI Planner (creates actions)
# =========================
def plan_actions(db: Session, batch_size: int = 25) -> Dict[str, Any]:
    run = AgentRun(mode="planning", status="STARTED", batch_size=batch_size, notes="")
    db.add(run)
    db.flush()

    planned = 0
    considered = 0
    now = _now()

    leads = (
        db.query(Lead)
        .filter(Lead.state == "NEW", Lead.phone.isnot(None))
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    for lead in leads:
        considered += 1
        first = safe_first_name(lead.full_name)

        # Action 1: TEXT (immediate)
        msg1 = (
            f"Hi{(' ' + first) if first else ''}, this is Nick’s office. "
            "You requested life insurance info before — totally okay if it’s been a while. "
            "Do you want help with a quick quote today?"
        )
        db.add(Action(
            lead_id=lead.id,
            type="TEXT",
            status="PENDING",
            tool="twilio",
            payload_json=json.dumps({"to": lead.phone, "body": msg1}),
            created_at=now,
        ))
        planned += 1

        # Action 2: CALL (later)
        db.add(Action(
            lead_id=lead.id,
            type="CALL",
            status="PENDING",
            tool="twilio",
            payload_json=json.dumps({"to": lead.phone, "lead_id": lead.id}),
            created_at=now,
        ))
        planned += 1

        lead.state = "WORKING"
        lead.last_contacted_at = now
        lead.updated_at = now

    run.status = "SUCCEEDED"
    run.finished_at = _now()
    db.commit()

    _log(db, None, run.id, "AI_PLANNED", f"planned={planned} considered={considered}")
    db.commit()

    return {"ok": True, "run_id": run.id, "planned_actions": planned, "considered": considered}

@app.get("/ai/plan")
def ai_plan():
    db = SessionLocal()
    try:
        out = plan_actions(db, batch_size=int(os.getenv("AI_BATCH_SIZE", "25")))
        return out
    finally:
        db.close()

# =========================
# Leads
# =========================
@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return HTMLResponse("Not found", status_code=404)

        actions = db.query(Action).filter(Action.lead_id == lead_id).order_by(Action.id.desc()).limit(100).all()
        logs = db.query(AuditLog).filter(AuditLog.lead_id == lead_id).order_by(AuditLog.created_at.desc()).limit(200).all()
        msgs = db.query(Message).filter(Message.lead_id == lead_id).order_by(Message.created_at.desc()).limit(50).all()

        action_html = ""
        for a in actions:
            action_html += f"""
            <div style="padding:10px 0;border-bottom:1px solid rgba(50,74,110,0.25)">
              <b>#{a.id} {a.type}</b> [{a.status}] <span style="opacity:.75">tool={a.tool}</span><br>
              <pre style="white-space:pre-wrap;margin:8px 0 0 0;">{a.payload_json}</pre>
              <div style="opacity:.85;color:#ffb4b4">{a.error or ""}</div>
            </div>
            """

        msg_html = ""
        for m in msgs:
            msg_html += f"""
            <div style="padding:10px 0;border-bottom:1px solid rgba(50,74,110,0.25)">
              <b>{m.direction}</b> {m.channel} <span style="opacity:.7">{m.created_at}</span><br>
              <div style="white-space:pre-wrap;opacity:.95">{m.body}</div>
            </div>
            """

        log_html = ""
        for l in logs:
            log_html += f"""
            <div style="padding:10px 0;border-bottom:1px solid rgba(50,74,110,0.25)">
              <b>{l.event}</b> <span style="opacity:.7">{l.created_at}</span><br>
              <div style="white-space:pre-wrap;opacity:.95">{l.detail}</div>
            </div>
            """

        return HTMLResponse(f"""
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1" /></head>
        <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:1100px;margin:0 auto;">
          <a href="/dashboard">← Back</a>
          <h2 style="margin:12px 0 6px 0;">#{lead.id} {lead.full_name or "Unknown"}</h2>
          <div>📞 {lead.phone or "—"}</div>
          <div>✉️ {lead.email or "—"}</div>
          <div style="opacity:.8">State: <b>{lead.state}</b></div>

          <div style="margin-top:14px; display:flex; gap:10px; flex-wrap:wrap;">
            <form method="post" action="/leads/delete/{lead.id}" onsubmit="return confirm('Delete this lead permanently?');">
              <button style="background:#b91c1c;border:none;color:white;padding:10px 14px;border-radius:12px;cursor:pointer;">🗑️ Delete Lead</button>
            </form>
          </div>

          <h3 style="margin-top:22px;">Messages</h3>
          <div style="background:#0f1624;padding:12px;border-radius:14px;border:1px solid rgba(50,74,110,0.25)">{msg_html or "<div style='opacity:.75'>No messages</div>"}</div>

          <h3 style="margin-top:22px;">Actions</h3>
          <div style="background:#0f1624;padding:12px;border-radius:14px;border:1px solid rgba(50,74,110,0.25)">{action_html or "<div style='opacity:.75'>No actions</div>"}</div>

          <h3 style="margin-top:22px;">Activity</h3>
          <div style="background:#0f1624;padding:12px;border-radius:14px;border:1px solid rgba(50,74,110,0.25)">{log_html or "<div style='opacity:.75'>No activity</div>"}</div>
        </body></html>
        """)
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

# =========================
# Lists: Actions / Activity
# =========================
@app.get("/actions", response_class=HTMLResponse)
def actions_page():
    db = SessionLocal()
    try:
        actions = db.query(Action).order_by(Action.id.desc()).limit(500).all()
        rows = ""
        for a in actions:
            rows += f"<div style='padding:10px 0;border-bottom:1px solid rgba(50,74,110,0.25)'>#{a.id} <b>{a.type}</b> lead={a.lead_id} [{a.status}] <span style='opacity:.7'>{a.created_at}</span> <div style='opacity:.8;color:#ffb4b4'>{(a.error or '')[:240]}</div></div>"
        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:1100px;margin:0 auto;">
        <a href="/dashboard">← Back</a>
        <h2>Action Queue</h2>
        <div style="background:#0f1624;padding:12px;border-radius:14px;border:1px solid rgba(50,74,110,0.25)">{rows or "No actions"}</div>
        </body></html>
        """)
    finally:
        db.close()

@app.get("/activity", response_class=HTMLResponse)
def activity():
    db = SessionLocal()
    try:
        logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(500).all()
        rows = ""
        for l in logs:
            rows += f"<div style='padding:10px 0;border-bottom:1px solid rgba(50,74,110,0.25)'><b>{l.event}</b> <span style='opacity:.7'>lead={l.lead_id} run={l.run_id} • {l.created_at}</span><div style='white-space:pre-wrap;opacity:.95'>{l.detail}</div></div>"
        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:1100px;margin:0 auto;">
        <a href="/dashboard">← Back</a>
        <h2>Activity</h2>
        <div style="background:#0f1624;padding:12px;border-radius:14px;border:1px solid rgba(50,74,110,0.25)">{rows or "No activity"}</div>
        </body></html>
        """)
    finally:
        db.close()

# =========================
# Admin (Mass Delete + Safety)
# =========================
@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse("""
    <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:900px;margin:0 auto;">
      <a href="/dashboard">← Back</a>
      <h2>🛡️ Admin</h2>
      <div style="opacity:.8;margin-bottom:10px">
        Dangerous actions require your ADMIN_TOKEN. You can pass it as a query param: <b>?token=YOURTOKEN</b>
      </div>

      <h3>⚠️ Delete ALL Leads</h3>
      <form method="post" action="/admin/wipe" onsubmit="return confirm('This deletes everything. Are you sure?');">
        <div style="opacity:.8">Type <b>DELETE ALL LEADS</b>:</div>
        <input name="confirm" style="padding:10px;border-radius:10px;border:1px solid #223047;background:#0f1624;color:#e6edf3;width:280px" />
        <button style="margin-left:10px;background:#b91c1c;color:white;border:none;padding:10px 14px;border-radius:12px;cursor:pointer;">
          Permanently Delete
        </button>
      </form>

      <h3 style="margin-top:18px">Clean Queue</h3>
      <form method="post" action="/admin/clear-actions" onsubmit="return confirm('Clear all actions?');">
        <button style="background:#111827;border:1px solid #223047;color:#e6edf3;padding:10px 14px;border-radius:12px;cursor:pointer;">
          Clear Actions Only
        </button>
      </form>
    </body></html>
    """)

@app.post("/admin/wipe")
async def admin_wipe(request: Request, confirm: str = Form(...)):
    if confirm.strip() != "DELETE ALL LEADS":
        return JSONResponse({"error": "Confirmation text does not match"}, status_code=400)
    if not require_admin(request):
        return JSONResponse({"error": "Missing/invalid ADMIN_TOKEN. Add ?token=YOUR_ADMIN_TOKEN"}, status_code=401)

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM messages"))
        db.execute(text("DELETE FROM audit_log"))
        db.execute(text("DELETE FROM lead_memory"))
        db.execute(text("DELETE FROM actions"))
        db.execute(text("DELETE FROM agent_runs"))
        db.execute(text("DELETE FROM leads"))
        db.commit()
        return JSONResponse({"ok": True, "message": "Everything deleted."})
    finally:
        db.close()

@app.post("/admin/clear-actions")
async def admin_clear_actions(request: Request):
    if not require_admin(request):
        return JSONResponse({"error": "Missing/invalid ADMIN_TOKEN. Add ?token=YOUR_ADMIN_TOKEN"}, status_code=401)
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM actions"))
        db.commit()
        return JSONResponse({"ok": True, "message": "Actions cleared."})
    finally:
        db.close()

# =========================
# Imports
# =========================
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
        <div>Service Account JSON<br><textarea name="creds_json" style="width:100%;height:140px"></textarea></div>
        <div>Spreadsheet ID<br><input name="spreadsheet_id" style="width:100%"/></div>
        <div>Range (example: Sheet1!A1:Z)<br><input name="range_name" style="width:100%"/></div>
        <button type="submit">Import</button>
      </form>

      <h3 style="margin-top:18px;">Google Drive CSV</h3>
      <form action="/import/drive-csv" method="post">
        <div>Service Account JSON<br><textarea name="creds_json" style="width:100%;height:140px"></textarea></div>
        <div>File ID<br><input name="file_id" style="width:100%"/></div>
        <button type="submit">Import</button>
      </form>

      <h3 style="margin-top:18px;">Google Doc</h3>
      <form action="/import/google-doc" method="post">
        <div>Service Account JSON<br><textarea name="creds_json" style="width:100%;height:140px"></textarea></div>
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

# =========================
# Twilio Webhooks (SMS + Recordings)
# =========================
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
            created_at=_now(),
        ))

        _log(db, lead.id, None, "SMS_IN", body)

        low = body.lower()

        # Compliance STOP
        if any(x in low for x in ["stop", "unsubscribe", "do not contact", "dont contact", "dnc"]):
            lead.state = "DO_NOT_CONTACT"
            lead.updated_at = _now()
            cancel_pending_actions(db, lead.id, "Inbound STOP/DNC")
            _log(db, lead.id, None, "COMPLIANCE_DNC", "Lead opted out via SMS")
            db.commit()
            return Response(content="<Response></Response>", media_type="text/xml")

        # Hot intent triggers alert to you
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

        if lead.state == "NEW":
            lead.state = "WORKING"
        lead.updated_at = _now()
        db.commit()
        return Response(content="<Response></Response>", media_type="text/xml")
    finally:
        db.close()

@app.post("/twilio/recording")
def twilio_recording(RecordingSid: str = Form(...), RecordingUrl: str = Form(...), CallSid: str = Form(...)):
    db = SessionLocal()
    try:
        playable = (RecordingUrl or "").strip()
        mp3 = playable + ".mp3" if playable and not playable.endswith(".mp3") else playable
        _log(db, None, None, "CALL_RECORDING", f"callSid={CallSid} recordingSid={RecordingSid} url={playable} mp3={mp3}")
        db.commit()
        return {"ok": True}
    finally:
        db.close()
