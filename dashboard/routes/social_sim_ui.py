from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from dashboard.templates_state import templates

router = APIRouter()


@router.get("/social-sim", response_class=HTMLResponse)
async def social_sim_view(request: Request):
    return templates.TemplateResponse(
        "social_sim.html",
        {"request": request, "active_page": "social_sim"},
    )
