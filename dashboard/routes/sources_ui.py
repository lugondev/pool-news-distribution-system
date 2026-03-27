"""HTML routes — Sources and Categories management (HTMX UI)."""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from dashboard.config_io import (
    get_categories,
    read_settings,
    read_sources,
    write_settings,
    write_sources,
)
from dashboard.templates_state import templates

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Sources ───────────────────────────────────────────────────────────────────


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    return templates.TemplateResponse("sources.html", {"request": request})


@router.get("/partials/sources", response_class=HTMLResponse)
async def sources_partial(request: Request):
    return templates.TemplateResponse(
        "partials/sources.html", {"request": request, "sources": read_sources()}
    )


@router.post("/sources/add", response_class=HTMLResponse)
async def source_add(
    request: Request,
    id: str = Form(...),
    name: str = Form(...),
    url: str = Form(...),
    lang: str = Form("en"),
    category: str = Form("world"),
):
    sources = read_sources()
    if any(s["id"] == id for s in sources):
        return templates.TemplateResponse(
            "partials/sources.html",
            {"request": request, "sources": sources, "error": f"Source '{id}' already exists"},
        )
    sources.append({"id": id, "name": name, "url": url, "type": "rss", "lang": lang, "category": category, "enabled": True})
    write_sources(sources)
    logger.info(f"Source added: {id}")
    return templates.TemplateResponse(
        "partials/sources.html",
        {"request": request, "sources": sources, "success": f"Source '{name}' added"},
    )


@router.post("/sources/{source_id}/toggle", response_class=HTMLResponse)
async def source_toggle(request: Request, source_id: str):
    sources = read_sources()
    for s in sources:
        if s["id"] == source_id:
            s["enabled"] = not s.get("enabled", True)
            break
    write_sources(sources)
    return templates.TemplateResponse("partials/sources.html", {"request": request, "sources": sources})


@router.delete("/sources/{source_id}", response_class=HTMLResponse)
async def source_delete(request: Request, source_id: str):
    sources = [s for s in read_sources() if s["id"] != source_id]
    write_sources(sources)
    logger.info(f"Source deleted: {source_id}")
    return templates.TemplateResponse(
        "partials/sources.html",
        {"request": request, "sources": sources, "success": f"Source '{source_id}' deleted"},
    )


@router.get("/sources/{source_id}/edit", response_class=HTMLResponse)
async def source_edit_form(request: Request, source_id: str):
    sources = read_sources()
    source = next((s for s in sources if s["id"] == source_id), None)
    if not source:
        return HTMLResponse("<tr><td colspan='6'>Source not found</td></tr>")
    return templates.TemplateResponse("partials/source_edit_row.html", {"request": request, "s": source})


@router.put("/sources/{source_id}", response_class=HTMLResponse)
async def source_update(
    request: Request,
    source_id: str,
    name: str = Form(...),
    url: str = Form(...),
    lang: str = Form("en"),
    category: str = Form("world"),
):
    sources = read_sources()
    for s in sources:
        if s["id"] == source_id:
            s["name"] = name
            s["url"] = url
            s["lang"] = lang
            s["category"] = category
            break
    write_sources(sources)
    logger.info(f"Source updated: {source_id}")
    return templates.TemplateResponse(
        "partials/sources.html",
        {"request": request, "sources": sources, "success": f"Source '{name}' updated"},
    )


# ── Categories ────────────────────────────────────────────────────────────────


@router.get("/partials/categories", response_class=HTMLResponse)
async def categories_partial(request: Request):
    return templates.TemplateResponse(
        "partials/categories.html", {"request": request, "categories": get_categories()}
    )


@router.post("/categories/add", response_class=HTMLResponse)
async def category_add(request: Request, id: str = Form(...), name: str = Form(...)):
    cfg = read_settings()
    cats = cfg.get("categories", [])
    if any(c["id"] == id for c in cats):
        return templates.TemplateResponse(
            "partials/categories.html",
            {"request": request, "categories": cats, "error": f"Category '{id}' already exists"},
        )
    cats.append({"id": id, "name": name, "enabled": True})
    cfg["categories"] = cats
    write_settings(cfg)
    return templates.TemplateResponse(
        "partials/categories.html",
        {"request": request, "categories": cats, "success": f"Category '{name}' added"},
    )


@router.post("/categories/{cat_id}/toggle", response_class=HTMLResponse)
async def category_toggle(request: Request, cat_id: str):
    cfg = read_settings()
    cats = cfg.get("categories", [])
    for c in cats:
        if c["id"] == cat_id:
            c["enabled"] = not c.get("enabled", True)
            break
    cfg["categories"] = cats
    write_settings(cfg)
    return templates.TemplateResponse("partials/categories.html", {"request": request, "categories": cats})


@router.delete("/categories/{cat_id}", response_class=HTMLResponse)
async def category_delete(request: Request, cat_id: str):
    cfg = read_settings()
    cats = [c for c in cfg.get("categories", []) if c["id"] != cat_id]
    cfg["categories"] = cats
    write_settings(cfg)
    return templates.TemplateResponse(
        "partials/categories.html",
        {"request": request, "categories": cats, "success": f"Category '{cat_id}' deleted"},
    )


@router.get("/partials/category-options", response_class=HTMLResponse)
async def category_options():
    cats = get_categories()
    html = "".join(f'<option value="{c["id"]}">{c["name"]}</option>' for c in cats)
    return HTMLResponse(html)
