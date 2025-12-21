import os
import textwrap

BASE_DIR = os.path.dirname(__file__)

files = {
    "app/__init__.py": """
from fastapi import FastAPI
""",
    "app/database.py": """
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# If you later deploy to the cloud with PostgreSQL, set DATABASE_URL there.
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Example for Postgres in the cloud:
    # postgres://user:password@host:port/dbname
    engine = create_engine(DATABASE_URL)
else:
    # Local dev: simple SQLite file
    engine = create_engine(
        "sqlite:///./crm.db",
        connect_args={"check_same_thread": False}
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
""",
    "app/models.py": """
from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from .database import Base
import datetime

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)

    leads = relationship("Lead", back_populates="owner")

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    full_name = Column(String(255), nullable=False)
    phone = Column(String(50))
    email = Column(String(255))
    status = Column(String(50), default="New")
    source = Column(String(255))
    notes = Column(Text)
    next_followup = Column(String(255))
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    owner = relationship("User", back_populates="leads")
""",
    "app/auth.py": """
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from hashlib import sha256

from .database import SessionLocal
from . import models

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def hash_password(password: str) -> str:
    # Simple hash for now. Later we can upgrade to bcrypt if you want.
    return sha256(password.encode("utf-8")).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed

@router.get("/register", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})

@router.post("/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    existing = db.query(models.User).filter(models.User.email == email).first()
    if existing:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Email already registered."},
            status_code=400,
        )

    user = models.User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)

    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=302)

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid email or password."},
            status_code=400,
        )

    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=302)

@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
""",
    "app/leads.py": """
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .database import SessionLocal
from . import models

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user_id(request: Request):
    return request.session.get("user_id")

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id(request)
    if not user_id:
        return RedirectResponse("/login", status_code=302)

    leads = (
        db.query(models.Lead)
        .filter(models.Lead.user_id == user_id)
        .order_by(models.Lead.created_at.desc())
        .all()
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "leads": leads},
    )

@router.post("/leads/add")
async def add_lead(
    request: Request,
    full_name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    status: str = Form("New"),
    source: str = Form(""),
    notes: str = Form(""),
    next_followup: str = Form(""),
    db: Session = Depends(get_db),
):
    user_id = get_current_user_id(request)
    if not user_id:
        return RedirectResponse("/login", status_code=302)

    lead = models.Lead(
        user_id=user_id,
        full_name=full_name,
        phone=phone,
        email=email,
        status=status,
        source=source,
        notes=notes,
        next_followup=next_followup,
    )
    db.add(lead)
    db.commit()

    return RedirectResponse("/dashboard", status_code=302)
""",
    "app/main.py": """
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .database import engine
from . import models
from .auth import router as auth_router
from .leads import router as leads_router

# Create DB tables
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Power CRM")

# Simple session middleware (stores session in signed cookie)
app.add_middleware(SessionMiddleware, secret_key="supersecret-key-change-me")

# Routes
app.include_router(auth_router)
app.include_router(leads_router)

# Static + templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)
""",
    "app/templates/base.html": """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Power CRM</title>
    <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
    <nav class="top-nav">
        <div class="brand">⚡ Power CRM</div>
        <div class="nav-links">
            <a href="/dashboard">Dashboard</a>
            <a href="/logout">Logout</a>
        </div>
    </nav>
    <main class="container">
        {% block content %}{% endblock %}
    </main>
</body>
</html>
""",
    "app/templates/login.html": """
{% extends "base.html" %}
{% block content %}
<div class="card">
    <h1>Login</h1>
    {% if error %}
        <div class="error">{{ error }}</div>
    {% endif %}
    <form method="post" action="/login" class="form">
        <label>Email</label>
        <input type="email" name="email" required>
        <label>Password</label>
        <input type="password" name="password" required>
        <button type="submit">Sign In</button>
    </form>
    <p class="muted">No account yet? <a href="/register">Register here</a></p>
</div>
{% endblock %}
""",
    "app/templates/register.html": """
{% extends "base.html" %}
{% block content %}
<div class="card">
    <h1>Create account</h1>
    {% if error %}
        <div class="error">{{ error }}</div>
    {% endif %}
    <form method="post" action="/register" class="form">
        <label>Email</label>
        <input type="email" name="email" required>
        <label>Password</label>
        <input type="password" name="password" required>
        <button type="submit">Register</button>
    </form>
    <p class="muted">Already have an account? <a href="/login">Log in</a></p>
</div>
{% endblock %}
""",
    "app/templates/dashboard.html": """
{% extends "base.html" %}
{% block content %}
<h1>Your Leads</h1>

<section class="card">
    <h2>Add Lead</h2>
    <form method="post" action="/leads/add" class="form grid-2">
        <div>
            <label>Full name</label>
            <input type="text" name="full_name" required>
        </div>
        <div>
            <label>Phone</label>
            <input type="text" name="phone">
        </div>
        <div>
            <label>Email</label>
            <input type="email" name="email">
        </div>
        <div>
            <label>Status</label>
            <select name="status">
                <option>New</option>
                <option>Contacted</option>
                <option>Follow-up</option>
                <option>Closed Won</option>
                <option>Closed Lost</option>
            </select>
        </div>
        <div>
            <label>Source</label>
            <input type="text" name="source" placeholder="TikTok, FB Ads, Referral...">
        </div>
        <div>
            <label>Next follow-up</label>
            <input type="text" name="next_followup" placeholder="Tomorrow 3pm, Next week...">
        </div>
        <div class="full-width">
            <label>Notes</label>
            <textarea name="notes" rows="3"></textarea>
        </div>
        <div class="full-width">
            <button type="submit">Save lead</button>
        </div>
    </form>
</section>

<section class="card">
    <h2>Lead List</h2>
    {% if leads %}
        <table class="leads-table">
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Phone</th>
                    <th>Status</th>
                    <th>Source</th>
                    <th>Next follow-up</th>
                </tr>
            </thead>
            <tbody>
                {% for lead in leads %}
                    <tr>
                        <td>{{ lead.full_name }}</td>
                        <td>{{ lead.phone }}</td>
                        <td>{{ lead.status }}</td>
                        <td>{{ lead.source }}</td>
                        <td>{{ lead.next_followup }}</td>
                    </tr>
                {% endfor %}
            </tbody>
        </table>
    {% else %}
        <p class="muted">No leads yet. Add your first one above.</p>
    {% endif %}
</section>
{% endblock %}
""",
    "app/static/styles.css": """
body {
    margin: 0;
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
    background: radial-gradient(circle at top, #10131a, #05060a);
    color: #f5f5f5;
}

.top-nav {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 24px;
    background: #05060a;
    border-bottom: 1px solid #222;
}

.top-nav .brand {
    font-weight: 700;
    letter-spacing: 0.03em;
}

.top-nav a {
    color: #ddd;
    text-decoration: none;
    margin-left: 16px;
    font-size: 0.9rem;
}

.top-nav a:hover {
    color: #fff;
}

.container {
    max-width: 960px;
    margin: 32px auto;
    padding: 0 16px;
}

.card {
    background: rgba(10, 12, 20, 0.96);
    border-radius: 16px;
    padding: 20px;
    margin-bottom: 24px;
    border: 1px solid rgba(255, 215, 0, 0.2);
    box-shadow: 0 18px 40px rgba(0, 0, 0, 0.45);
}

h1, h2 {
    margin-top: 0;
}

.form {
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.form input,
.form select,
.form textarea {
    background: #05060a;
    border-radius: 10px;
    border: 1px solid #333;
    padding: 8px 10px;
    color: #f5f5f5;
    font-size: 0.95rem;
}

.form input:focus,
.form select:focus,
.form textarea:focus {
    outline: none;
    border-color: gold;
}

.form button {
    margin-top: 8px;
    padding: 10px 14px;
    border-radius: 999px;
    border: none;
    background: linear-gradient(135deg, gold, #ffb347);
    color: #05060a;
    font-weight: 600;
    cursor: pointer;
}

.form button:hover {
    filter: brightness(1.05);
}

.muted {
    color: #888;
    font-size: 0.85rem;
}

.error {
    background: rgba(220, 53, 69, 0.1);
    border-radius: 8px;
    padding: 8px 10px;
    border: 1px solid rgba(220, 53, 69, 0.6);
    color: #fca5a5;
    margin-bottom: 8px;
}

.grid-2 {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px 16px;
}

.grid-2 .full-width {
    grid-column: 1 / -1;
}

.leads-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
}

.leads-table th,
.leads-table td {
    padding: 8px 6px;
    border-bottom: 1px solid #222;
}

.leads-table th {
    text-align: left;
    color: #aaa;
    font-weight: 500;
}
"""
}

for rel_path, content in files.items():
    full_path = os.path.join(BASE_DIR, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(content).lstrip("\\n"))

print("✅ Project files created.")
