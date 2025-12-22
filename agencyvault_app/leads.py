from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .database import SessionLocal
from . import models

router = APIRouter()
templates = Jinja2Templates(directory="agencyvault_app/templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db: Session):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(models.User).get(user_id)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    leads_count = db.query(models.Lead).filter(models.Lead.owner_id == user.id).count()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "leads_count": leads_count,
        },
    )


@router.get("/leads/new", response_class=HTMLResponse)
async def new_lead_form(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    return templates.TemplateResponse(
        "lead_form.html",
        {"request": request, "user": user},
    )


@router.post("/leads/new")
async def create_lead(
    request: Request,
    full_name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    status: str = Form("New"),
    source: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    lead = models.Lead(
        owner_id=user.id,
        full_name=full_name,
        phone=phone,
        email=email,
        status=status,
        source=source,
        notes=notes,
    )
    db.add(lead)
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/leads", response_class=HTMLResponse)
async def list_leads(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    leads = (
        db.query(models.Lead)
        .filter(models.Lead.owner_id == user.id)
        .order_by(models.Lead.id.desc())
        .all()
    )

    return templates.TemplateResponse(
        "leads.html",
        {"request": request, "user": user, "leads": leads},
    )
