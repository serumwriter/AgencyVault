from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session
from fastapi.templating import Jinja2Templates

from .database import SessionLocal
from .models import Lead

router = APIRouter()
templates = Jinja2Templates(directory="agencyvault_app/templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    leads = db.query(Lead).all()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "leads": leads}
    )


@router.post("/leads")
def create_lead(
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    notes: str = Form(None),
    db: Session = Depends(get_db)
):
    lead = Lead(
        name=name,
        phone=phone,
        email=email,
        notes=notes
    )

    db.add(lead)
    db.commit()

    return RedirectResponse("/dashboard", status_code=303)

