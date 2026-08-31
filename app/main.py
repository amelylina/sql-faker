from pathlib import Path
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import generate_batch, list_locales

app = FastAPI(title="SQL Faker")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    locale: str | None = None,
    seed: int | None = Query(default=42, ge=1, le=1_000_000),
    batch: int = 0,
    batch_size: int = 10
):
    locales = list_locales()
    users = None
    if locale and seed is not None:
        users = generate_batch(locale,seed,batch,batch_size)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "locales": locales,
            "selected_locale": locale,
            "seed": seed,
            "batch": batch,
            "batch_size": batch_size,
            "users": users,
        },
    )
