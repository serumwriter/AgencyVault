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

app = FastAPI(title="AgencyVault")

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

