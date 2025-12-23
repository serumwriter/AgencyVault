from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .database import engine
from . import models
from .auth import router as auth_router
from .leads import router as leads_router

app = FastAPI(title="AgencyVault")

@app.on_event("startup")
def on_startup():
    models.Base.metadata.create_all(bind=engine)

# Sessions
app.add_middleware(
    SessionMiddleware,
    secret_key="CHANGE_THIS_SECRET_IN_PROD"
)

# Routers
app.include_router(auth_router)
app.include_router(leads_router)

# Static & templates
app.mount("/static", StaticFiles(directory="agencyvault_app/static"), name="static")
templates = Jinja2Templates(directory="agencyvault_app/templates")

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)

from sqlalchemy import text
from fastapi.responses import HTMLResponse

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login", status_code=302)

    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT id, first_name, last_name, phone, email FROM leads ORDER BY id DESC")
        )
        rows = result.fetchall()

    leads = []
    for r in rows:
        leads.append({
            "full_name": f"{r.first_name or ''} {r.last_name or ''}".strip(),
            "phone": r.phone,
            "email": r.email
        })

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "leads": leads}
    )

