from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, text
from sqlalchemy.orm import sessionmaker, declarative_base
from datetime import datetime
import csv
import os
import re

from .ai_employee import run_ai_engine
from ai_tasks import create_task

# --------------------
# DATABASE
# --------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    full_name = Column(String)
    phone = Column(String)
    email = Column(String)

    state = Column(String, default="NEW")
    ai_priority = Column(Integer)
    ai_next_action = Column(String)
    ai_reason = Column(Text)
    ai_last_action_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# --------------------
# APP
# --------------------
app = FastAPI()

# --------------------
# HELPERS
# --------------------
def normalize_phone(s):
    d = re.sub(r"\D", "", s or "")
    if len(d) == 10:
        return "+1" + d
    return d

def looks_like_phone(s):
    return len(re.sub(r"\D", "", s or "")) == 10

def looks_like_name(s):
    parts = s.split()
    return len(parts) >= 2 and all(p.isalpha() for p in parts)

# --------------------
# ROUTES
# --------------------
@app.get("/")
def root():
    return RedirectResponse("/dashboard")

# ---------- DASHBOARD ----------
@app.get("/dashboard")
def dashboard():
    db = SessionLocal()
    leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()
    db.close()

    rows = ""
    for l in leads:
        rows += f"""
        <div class="card">
          <b>{l.full_name}</b><br>
          {l.phone}<br>
          {l.email or ""}
        </div>
        """

    return HTMLResponse(
        "<html><head><style>"
        "body{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}"
        ".card{background:#111827;padding:16px;margin:12px 0;border-radius:10px}"
        "</style></head><body>"
        "<h2>Leads</h2>"
        "<form method='post' action='/leads/upload' enctype='multipart/form-data'>"
        "<input type='file' name='file' required>"
        "<button>Upload CSV</button>"
        "</form>"
        + rows +
        "<br><a href='/tasks'>View Tasks</a>"
        "</body></html>"
    )

# ---------- LEAD DETAIL ----------
@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    db.close()

    if not lead:
        return HTMLResponse("Lead not found", status_code=404)

    return HTMLResponse(f"""
    <html>
    <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">

      <h2>{lead.full_name}</h2>

      <p><b>Phone:</b> {lead.phone}</p>
      <p><b>Email:</b> {lead.email or "—"}</p>
      <p><b>Status:</b> {lead.state}</p>

      <hr>

      <form method="post" action="/leads/{lead.id}/call">
        <button style="padding:10px;margin:6px 0;">
          📞 CALL (Dry Run)
        </button>
      </form>

      <hr>

      <h3>Escalate to Human</h3>

      <form method="post" action="/leads/{lead.id}/escalate/now">
        <button style="background:#dc2626;color:white;padding:10px;margin:6px 0;">
          🔥 Wants Coverage NOW
        </button>
      </form>

      <form method="post" action="/leads/{lead.id}/escalate/problem">
        <button style="background:#f59e0b;color:black;padding:10px;margin:6px 0;">
          ⚠️ Confused / Upset / Complicated
        </button>
      </form>

      <br>
      <a href="/tasks">← Back to Tasks</a>

    </body>
    </html>
    """)

# ---------- CALL (DRY RUN) ----------
@app.post("/leads/{lead_id}/call")
def call_lead_dry_run(lead_id: int):
    create_task("CALL", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

# ---------- ESCALATION ----------
@app.post("/leads/{lead_id}/escalate/now")
def escalate_now(lead_id: int):
    create_task("ESCALATE_NOW", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

@app.post("/leads/{lead_id}/escalate/problem")
def escalate_problem(lead_id: int):
    create_task("ESCALATE_PROBLEM", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

# ---------- CSV UPLOAD ----------
@app.post("/leads/upload")
def upload(file: UploadFile = File(...)):
    raw = file.file.read().decode("utf-8", errors="ignore").splitlines()
    rows = csv.reader(raw)

    db = SessionLocal()
    count = 0

    for r in rows:
        vals = [c.strip() for c in r if c.strip()]
        name = next((v for v in vals if looks_like_name(v)), None)
        phone = next((v for v in vals if looks_like_phone(v)), None)

        if not name or not phone:
            continue

        db.add(Lead(
            full_name=name,
            phone=normalize_phone(phone),
            state="NEW"
        ))
        count += 1

    db.commit()
    db.close()

    return HTMLResponse(f"<h3>Imported {count}</h3><a href='/dashboard'>Back</a>")

# ---------- AI PLAN ----------
@app.get("/ai/run")
def ai_run():
    db = SessionLocal()
    actions = run_ai_engine(db, Lead)

    for a in actions:
    action_type = a["type"]

    if action_type in ("CALL", "SUGGEST_CALL"):
        create_task("CALL", a["lead_id"])

    elif action_type.startswith("ESCALATE"):
        create_task(action_type, a["lead_id"])

    # ignore internal AI events like LEAD_TRIAGED


    db.close()
    return {"planned": len(actions)}

# ---------- TASKS ----------
@app.get("/tasks")
def tasks():
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT t.task_type, t.lead_id, l.full_name, l.phone
        FROM ai_tasks t
        JOIN leads l ON l.id = t.lead_id
        WHERE t.status = 'NEW'
        ORDER BY
          CASE
            WHEN t.task_type LIKE 'ESCALATE%' THEN 0
            ELSE 1
          END,
          t.created_at
        LIMIT 50
    """)).fetchall()
    db.close()

    cards = ""
    for r in rows:
        color = "#dc2626" if r.task_type.startswith("ESCALATE") else "#111827"
        cards += (
            f"<div class='card' style='border-left:6px solid {color}'>"
            f"<b>{r.task_type}</b><br>"
            f"{r.full_name}<br>"
            f"{r.phone}<br>"
            f"<a href='/leads/{r.lead_id}'>View Lead →</a>"
            "</div>"
        )

    return HTMLResponse(
        "<html><head><style>"
        "body{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}"
        ".card{background:#111827;padding:16px;margin:12px 0;border-radius:10px}"
        "</style></head><body>"
        "<h2>Tasks</h2>"
        + cards +
        "<br><a href='/dashboard'>Back</a>"
        "</body></html>"
    )
