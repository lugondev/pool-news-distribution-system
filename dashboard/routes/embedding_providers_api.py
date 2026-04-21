"""JSON API — Embedding Providers CRUD and management."""

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dashboard.config_io import read_settings, write_settings

logger = logging.getLogger(__name__)
router = APIRouter()


class EmbeddingProviderIn(BaseModel):
    id: str
    name: str
    api_key: str
    base_url: str
    model: str


@router.get("/embedding-providers")
async def list_embedding_providers():
    """List all embedding providers."""
    cfg = read_settings()
    embedding_cfg = cfg.get("embedding", {})
    providers = embedding_cfg.get("providers", [])
    active_id = embedding_cfg.get("active_provider_id")
    
    return {
        "providers": providers,
        "active_provider_id": active_id,
    }


@router.post("/embedding-providers")
async def create_embedding_provider(body: EmbeddingProviderIn):
    """Create a new embedding provider."""
    cfg = read_settings()
    embedding_cfg = cfg.setdefault("embedding", {})
    providers = embedding_cfg.setdefault("providers", [])
    
    # Check for duplicate ID
    if any(p.get("id") == body.id for p in providers):
        raise HTTPException(status_code=400, detail=f"Provider ID '{body.id}' already exists")
    
    new_provider = {
        "id": body.id,
        "name": body.name,
        "api_key": body.api_key,
        "base_url": body.base_url,
        "model": body.model,
    }
    
    providers.append(new_provider)
    
    # Set as active if it's the first provider
    if len(providers) == 1:
        embedding_cfg["active_provider_id"] = body.id
    
    write_settings(cfg)
    logger.info(f"Created embedding provider: {body.id}")
    
    return {"ok": True, "provider": new_provider}


@router.put("/embedding-providers/{provider_id}")
async def update_embedding_provider(provider_id: str, body: EmbeddingProviderIn):
    """Update an existing embedding provider."""
    cfg = read_settings()
    embedding_cfg = cfg.get("embedding", {})
    providers = embedding_cfg.get("providers", [])
    
    # Find provider
    provider = None
    for p in providers:
        if p.get("id") == provider_id:
            provider = p
            break
    
    if not provider:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
    
    # Update fields
    provider["id"] = body.id
    provider["name"] = body.name
    provider["api_key"] = body.api_key
    provider["base_url"] = body.base_url
    provider["model"] = body.model
    
    # Update active_provider_id if ID changed
    if embedding_cfg.get("active_provider_id") == provider_id and body.id != provider_id:
        embedding_cfg["active_provider_id"] = body.id
    
    write_settings(cfg)
    logger.info(f"Updated embedding provider: {provider_id} → {body.id}")
    
    return {"ok": True, "provider": provider}


@router.delete("/embedding-providers/{provider_id}")
async def delete_embedding_provider(provider_id: str):
    """Delete an embedding provider."""
    cfg = read_settings()
    embedding_cfg = cfg.get("embedding", {})
    providers = embedding_cfg.get("providers", [])
    
    # Find and remove provider
    initial_count = len(providers)
    providers[:] = [p for p in providers if p.get("id") != provider_id]
    
    if len(providers) == initial_count:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
    
    # Update active provider if deleted
    if embedding_cfg.get("active_provider_id") == provider_id:
        embedding_cfg["active_provider_id"] = providers[0]["id"] if providers else None
    
    write_settings(cfg)
    logger.info(f"Deleted embedding provider: {provider_id}")
    
    return {"ok": True, "deleted": provider_id}


@router.put("/embedding/active-provider")
async def set_active_embedding_provider(provider_id: str):
    """Set the active embedding provider."""
    cfg = read_settings()
    embedding_cfg = cfg.setdefault("embedding", {})
    providers = embedding_cfg.get("providers", [])
    
    # Verify provider exists
    if not any(p.get("id") == provider_id for p in providers):
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
    
    embedding_cfg["active_provider_id"] = provider_id
    write_settings(cfg)
    logger.info(f"Set active embedding provider: {provider_id}")
    
    return {"ok": True, "active_provider_id": provider_id}


@router.post("/embedding-providers/{provider_id}/test")
async def test_embedding_provider(provider_id: str):
    """Test an embedding provider connection."""
    cfg = read_settings()
    embedding_cfg = cfg.get("embedding", {})
    providers = embedding_cfg.get("providers", [])
    
    # Find provider
    provider = None
    for p in providers:
        if p.get("id") == provider_id:
            provider = p
            break
    
    if not provider:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not found")
    
    # Test connection (simple validation for now)
    try:
        from openai import AsyncOpenAI
        
        client = AsyncOpenAI(
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            timeout=10.0,
        )
        
        # Try to create an embedding
        response = await client.embeddings.create(
            model=provider["model"],
            input="test",
        )
        
        if response.data and len(response.data) > 0:
            dim = len(response.data[0].embedding)
            return {
                "ok": True,
                "message": f"Connection successful! Model: {provider['model']}, Dimension: {dim}",
                "dimension": dim,
            }
        else:
            raise ValueError("No embedding data returned")
    
    except Exception as e:
        logger.error(f"Embedding provider test failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Test failed: {str(e)}")
