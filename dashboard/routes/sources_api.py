"""JSON API — Sources and Categories CRUD."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dashboard.config_io import (
    read_settings,
    read_sources,
    write_settings,
    write_sources,
)


# Auth gating: all mutating endpoints require manager role.
from fastapi import Depends as _Depends
from auth import require_role as _require_role
_mgr = [_Depends(_require_role("manager"))]

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Sources ──────────────────────────────────────────────────────────────────


class SourceIn(BaseModel):
    id: str
    name: str
    url: str
    lang: str = "en"
    category: str = "world"


class SourceUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    lang: str | None = None
    category: str | None = None


@router.get("/sources")
async def list_sources():
    return {"sources": read_sources()}


@router.post("/sources", status_code=201, dependencies=_mgr)
async def add_source(body: SourceIn):
    sources = read_sources()
    if any(s["id"] == body.id for s in sources):
        raise HTTPException(409, f"Source '{body.id}' already exists")
    entry = {
        "id": body.id,
        "name": body.name,
        "url": body.url,
        "type": "rss",
        "lang": body.lang,
        "category": body.category,
        "enabled": True,
    }
    sources.append(entry)
    write_sources(sources)
    logger.info(f"API: source added: {body.id}")
    return {"ok": True, "source": entry}


@router.put("/sources/{source_id}", dependencies=_mgr)
async def update_source(source_id: str, body: SourceUpdate):
    sources = read_sources()
    target = next((s for s in sources if s["id"] == source_id), None)
    if not target:
        raise HTTPException(404, "Source not found")
    for field in ("name", "url", "lang", "category"):
        val = getattr(body, field)
        if val is not None:
            target[field] = val
    write_sources(sources)
    logger.info(f"API: source updated: {source_id}")
    return {"ok": True, "source": target}


@router.post("/sources/{source_id}/toggle", dependencies=_mgr)
async def toggle_source(source_id: str):
    sources = read_sources()
    target = next((s for s in sources if s["id"] == source_id), None)
    if not target:
        raise HTTPException(404, "Source not found")
    target["enabled"] = not target.get("enabled", True)
    write_sources(sources)
    return {"ok": True, "source": target}


@router.delete("/sources/{source_id}", dependencies=_mgr)
async def delete_source(source_id: str):
    sources = read_sources()
    new = [s for s in sources if s["id"] != source_id]
    if len(new) == len(sources):
        raise HTTPException(404, "Source not found")
    write_sources(new)
    logger.info(f"API: source deleted: {source_id}")
    return {"ok": True}


# ── Categories ───────────────────────────────────────────────────────────────


class CategoryIn(BaseModel):
    id: str
    name: str


@router.get("/categories")
async def list_categories():
    return {"categories": read_settings().get("categories", [])}


@router.post("/categories", status_code=201, dependencies=_mgr)
async def add_category(body: CategoryIn):
    cfg = read_settings()
    cats = cfg.get("categories", [])
    if any(c["id"] == body.id for c in cats):
        raise HTTPException(409, f"Category '{body.id}' already exists")
    cats.append({"id": body.id, "name": body.name, "enabled": True})
    cfg["categories"] = cats
    write_settings(cfg)
    return {"ok": True, "category": cats[-1]}


@router.post("/categories/{cat_id}/toggle", dependencies=_mgr)
async def toggle_category(cat_id: str):
    cfg = read_settings()
    cats = cfg.get("categories", [])
    target = next((c for c in cats if c["id"] == cat_id), None)
    if not target:
        raise HTTPException(404, "Category not found")
    target["enabled"] = not target.get("enabled", True)
    cfg["categories"] = cats
    write_settings(cfg)
    return {"ok": True, "category": target}


@router.delete("/categories/{cat_id}", dependencies=_mgr)
async def delete_category(cat_id: str):
    cfg = read_settings()
    cats = cfg.get("categories", [])
    new = [c for c in cats if c["id"] != cat_id]
    if len(new) == len(cats):
        raise HTTPException(404, "Category not found")
    cfg["categories"] = new
    write_settings(cfg)
    return {"ok": True}
