"""Auth + RBAC subsystem.

See `docs/superpowers/specs/2026-04-28-auth-rbac-design.md` for the design.

Public surface:
    from auth import (
        get_auth_store, init_auth_db,
        require_login, require_role, require_perm,
        AuthMiddleware,
        User, ROLES, PERMISSIONS,
    )
"""

from auth.store import User, get_auth_store, init_auth_db, ROLES, PERMISSIONS
from auth.middleware import AuthMiddleware
from auth.deps import require_login, require_role, require_perm

__all__ = [
    "User", "ROLES", "PERMISSIONS",
    "get_auth_store", "init_auth_db",
    "require_login", "require_role", "require_perm",
    "AuthMiddleware",
]
