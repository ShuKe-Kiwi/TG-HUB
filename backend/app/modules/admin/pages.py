"""Server-rendered shell pages for the local administration console."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

_MODULE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_MODULE_DIR / "templates")
static_directory = _MODULE_DIR / "static"

pages_router = APIRouter(prefix="/admin")


def _context(request: Request, *, page: str, title: str) -> dict[str, object]:
    return {
        "request": request,
        "page": page,
        "title": title,
        "csrf_token": request.app.state.admin_csrf_token,
    }


@pages_router.get("", include_in_schema=False)
async def admin_root() -> RedirectResponse:
    return RedirectResponse(url="/admin/", status_code=307)


@pages_router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def admin_overview(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="overview.html",
        context=_context(request, page="overview", title="运行概览"),
    )


@pages_router.get(
    "/watchlist",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def admin_watchlist(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="watchlist.html",
        context=_context(request, page="watchlist", title="监听配置"),
    )
