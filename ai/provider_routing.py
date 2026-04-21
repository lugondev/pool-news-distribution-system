"""
AI Provider Routing — resolve provider credentials based on action type.

Actions: rewrite, synthesis, debate, newsletter, embedding
"""

from dashboard.config_io import cached_yaml


def get_provider_for_action(action: str) -> tuple[str, str, str]:
    """
    Get (api_key, base_url, model) for the specified action.
    
    Args:
        action: One of 'rewrite', 'synthesis', 'debate', 'newsletter', 'embedding'
        
    Returns:
        (api_key, base_url, model) tuple
        
    For embedding action:
        - If routing is 'system', uses first provider's credentials + embedding_model fallback
        - If routing is a provider_id, uses that provider's embedding_model (or model if not set)
    """
    cfg = cached_yaml("config/settings.yaml")
    ai_cfg = cfg.get("ai", {})
    
    # Get routing config
    routing = ai_cfg.get("provider_routing", {})
    provider_id = routing.get(action)
    
    # Special handling for embedding action
    if action == "embedding":
        # If routing is 'system', use first provider
        if provider_id == "system" or not provider_id:
            providers = ai_cfg.get("providers", [])
            if providers:
                p = providers[0]
                # Try embedding_model first, fallback to processing.embedding_model
                model = p.get("embedding_model")
                if not model:
                    processing_cfg = cfg.get("processing", {})
                    model = processing_cfg.get("embedding_model", "text-embedding-3-small")
                return (
                    p.get("api_key", ""),
                    p.get("base_url", "https://api.openai.com/v1"),
                    model,
                )
        # Otherwise find the specified provider
        else:
            for p in ai_cfg.get("providers", []):
                if p.get("id") == provider_id:
                    # Use embedding_model if available, otherwise fall back to model
                    model = p.get("embedding_model") or p.get("model", "")
                    return (
                        p.get("api_key", ""),
                        p.get("base_url", "https://api.openai.com/v1"),
                        model,
                    )
    
    # Fallback to legacy provider_id if routing not configured
    if not provider_id:
        provider_id = ai_cfg.get("provider_id")
    
    # Find provider by ID
    if provider_id:
        for p in ai_cfg.get("providers", []):
            if p.get("id") == provider_id:
                return (
                    p.get("api_key", ""),
                    p.get("base_url", "https://api.openai.com/v1"),
                    p.get("model", ""),
                )
    
    # Fallback to top-level config (legacy)
    return (
        ai_cfg.get("api_key", ""),
        ai_cfg.get("base_url", "https://api.openai.com/v1"),
        ai_cfg.get("model", ""),
    )
