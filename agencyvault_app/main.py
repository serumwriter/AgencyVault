from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy import text
from datetime import datetime, timedelta
import json
import re

from .database import SessionLocal, engine
from .models import Lead, Action, AuditLog
from .ai_employee import plan_actions

app = FastAPI(title="AgencyVault")

# --------------------------------------------------
# Utils
# --------------------------------------------------

def now():
    return datetime.utcnow()

def log(db, event, detail="", lead_id=None):
    db.add(AuditLog(
        lead_id=lead_id,
        event=event,
        detail=detail[:5000],
        created_at=now()
    ))

# --------------------------------------------------
# Health
# --------------------------------------------------

@app.get("/health")
def health():
    with engine.begin() as conn:
        conn.execute(text("select 1"))
    return {"ok": True}

@app.get("/")
def root():
    return RedirectResponse("/dashboard")

# --------------------------------------------------
# DASHBOARD (PREMIUM UI)
# --------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    try:
        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()
        pending = db.query(Action).filter(Action.status=="PENDING").count()

        rows = ""
        for l in leads:
            rows += f"""
            <tr>
              <td>{l.id}</td>
              <td>{l.full_name or "Unknown"}</td>
              <td>{l.phone}</td>
              <td>{l.state}</td>
              <td>
                <form method="post" action="/leads/delete/{l.id}" style="display:inline">
                  <button class="danger">Delete</button>
                </form>
              </td>
            </tr>
            """

        return f"""
<!doctype html>
<html>
<head>
<title>AgencyVault Command Center</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{
  background:#0b0f17; color:#e6edf3; font-family:system-ui;
  padding:20px; max-width:1200px; margin:auto;
}}
h1 {{ font-size:28px; }}
table {{ width:100%; border-collapse:collapse; margin-top:15px; }}
th,td {{ padding:10px; border-bottom:1px solid #1f2b3e; }}
th {{ text-align:left; opacity:.7; }}
button {{
  background:#111827; color:#fff;
  border:1px solid #223047;
  padding:6px 10px; border-radius:8px;
}}
.danger {{ background:#2a0f14; border-color:#5b1a22; }}
textarea {{
  width:100%; min-height:80px;
  background:#0b1220; color:#e6edf3;
  border:1px solid #223047;
  border-radius:10px; padding:10px;
}}
.chat {{
  background:#0b1220;
  border:1px solid #223047;
  border-radius:14px;
  padding:12px;
  margin-top:20px;
}}
</style>
</head>
<body>

<h1>AgencyVault — Command Center</h1>

<p>Pending actions: <b>{pending}</b></p>

<form method="post" action="/admin/delete-all-leads"
 onsubmit="return confirm('THIS WILL DELETE ALL LEADS. TYPE CONFIRM ON NEXT SCREEN.')">
 <button class="danger">☢️ MASS DELETE ALL LEADS</button>
</form>

<table>
<tr>
<th>ID</th><th>Name</th><th>Phone</th><th>State</th><th>Action</th>
</tr>
{rows}
</table>

<div class="chat">
<b>🤖 AI Employee</b>
<textarea id="msg" placeholder="Ask me anything about your system..."></textarea>
<button onclick="send()">Send</button>
<pre id="out"></pre>
</div>

<script>
async function send() {{
  const msg = document.getElementById("msg").value;
  const out = document.getElementById("out");
  out.textContent = "Thinking...";
  const r = await fetch("/api/assistant", {{
    method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body:JSON.stringify({{message:msg}})
  }});
  const d = await r.json();
  out.textContent = d.reply;
}}
</script>

</body>
</html>
"""
    finally:
        db.close()

# --------------------------------------------------
# AI CHAT (LIKE CHATGPT)
# --------------------------------------------------

@app.post("/api/assistant")
def assistant(payload: dict):
    msg = (payload.get("message") or "").lower()
    db = SessionLocal()
    try:
        log(db, "AI_CHAT", msg)
        db.commit()

        if "run" in msg:
            out = plan_actions(db, batch_size=25)
            db.commit()
            return {"reply": f"Planner ran. Planned {out['planned_actions']} actions."}

        if "bad lead" in msg or "delete junk" in msg:
            bad = db.query(Lead).filter(Lead.state=="DO_NOT_CONTACT").count()
            return {"reply": f"I recommend deleting {bad} DNC leads. Use mass delete to confirm."}

        if "how many" in msg:
            total = db.query(Lead).count()
            return {"reply": f"You have {total} leads."}

        return {"reply": "I’m your AI employee. I can run outreach, explain actions, flag bad leads, and protect compliance."}
    finally:
        db.close()

# --------------------------------------------------
# DELETE CONTROLS
# --------------------------------------------------

@app.post("/leads/delete/{lead_id}")
def delete_lead(lead_id: int):
    db = SessionLocal()
    try:
        db.execute(text("delete from actions where lead_id=:id"), {"id":lead_id})
        db.execute(text("delete from leads where id=:id"), {"id":lead_id})
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()

@app.post("/admin/delete-all-leads")
def delete_all():
    return HTMLResponse("""
    <form method="post" action="/admin/delete-all-leads/confirm">
      <h3>Type DELETE ALL LEADS</h3>
      <input name="confirm"/>
      <button>Confirm</button>
    </form>
    """)

@app.post("/admin/delete-all-leads/confirm")
def delete_all_confirm(confirm: str = Form(...)):
    if confirm != "DELETE ALL LEADS":
        return {"error":"confirmation failed"}
    db = SessionLocal()
    try:
        db.execute(text("delete from actions"))
        db.execute(text("delete from leads"))
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()
