"""
Mtime-based YAML config cache — tránh đọc disk mỗi request.

Reload tự động khi file thay đổi (mtime khác), transparent với caller.
Thread-safe với asyncio (single-threaded event loop).
"""

import os
from typing import Any

import yaml

# path → (mtime, parsed_data)
_cache: dict[str, tuple[float, Any]] = {}


def cached_yaml(path: str) -> Any:
    """Return parsed YAML, reloading only when file mtime changes."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}

    if path in _cache and _cache[path][0] == mtime:
        return _cache[path][1]

    with open(path) as f:
        data = yaml.safe_load(f) or {}
    _cache[path] = (mtime, data)
    return data


def invalidate(path: str) -> None:
    """Force reload on next access (call after write_settings / write_sources)."""
    _cache.pop(path, None)
