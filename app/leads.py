
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
