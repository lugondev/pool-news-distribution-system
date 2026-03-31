from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from dashboard.templates_state import templates

router = APIRouter()


@router.get("/social-agents", response_class=HTMLResponse)
async def social_agents_view(request: Request):
    return templates.TemplateResponse(
        "social_agents.html",
        {"request": request, "active_page": "social_agents"},
    )
