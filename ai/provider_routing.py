"""
AI Provider Routing — resolve provider credentials based on action type.

Actions: rewrite, synthesis, debate, newsletter, embedding
"""

from dashboard.config_io import read_settings


def get_provider_for_action(action: str) -> tuple[str, str, str]:
    """
    Get (api_key, base_url, model) for the specified action.
    
    Args:
        action: One of 'rewrite', 'synthesis', 'debate', 'newsletter', 'embedding'
        
    Returns:
        (api_key, base_url, model) tuple
        
    For embedding action:
        - Uses dedicated embedding.providers section
        - Requires active_provider_id to be configured
        - Raises ValueError if not configured or provider not found
    """
    cfg = read_settings()
    
    # Special handling for embedding action
    if action == "embedding":
        embedding_cfg = cfg.get("embedding", {})
        providers = embedding_cfg.get("providers", [])
        active_id = embedding_cfg.get("active_provider_id")
        
        # Must have active_provider_id configured
        if not active_id:
            raise ValueError("No active embedding provider configured. Please set one in /embedding-providers")
        
        # Find active provider
        for p in providers:
            if p.get("id") == active_id:
                return (
                    p.get("api_key", ""),
                    p.get("base_url", "https://api.openai.com/v1"),
                    p.get("model", "text-embedding-3-small"),
                )
        
        raise ValueError(f"Active embedding provider '{active_id}' not found in providers list")
    
    # For other actions (rewrite, synthesis, debate, newsletter)
    ai_cfg = cfg.get("ai", {})
    
    # Get routing config
    routing = ai_cfg.get("provider_routing", {})
    provider_id = routing.get(action)
    
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
