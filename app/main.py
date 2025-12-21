
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
