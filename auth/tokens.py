"""Personal Access Tokens.

Format: nag_pat_<48 hex chars> (24 random bytes).
Stored as sha256 hex in DB. Plaintext shown to user once at generation.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

PAT_PREFIX_LITERAL = "nag_pat_"
_RANDOM_BYTES = 24                                # → 48 hex chars
_DISPLAY_PREFIX_LEN = len(PAT_PREFIX_LITERAL) + 4  # "nag_pat_xxxx" = 12 chars


def generate_pat() -> tuple[str, str, str]:
    """Return (plaintext, sha256_hex, display_prefix).

    plaintext      → show to user once, never store
    sha256_hex     → store in DB
    display_prefix → first 12 chars of plaintext, safe to show in UI listings
    """
    body = secrets.token_hex(_RANDOM_BYTES)
    plaintext = f"{PAT_PREFIX_LITERAL}{body}"
    return plaintext, hash_pat(plaintext), plaintext[:_DISPLAY_PREFIX_LEN]


def hash_pat(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def looks_like_pat(value: str) -> bool:
    """Quick shape check before DB lookup. Avoids hashing arbitrary strings."""
    if not value or not value.startswith(PAT_PREFIX_LITERAL):
        return False
    body = value[len(PAT_PREFIX_LITERAL):]
    return len(body) == _RANDOM_BYTES * 2 and all(c in "0123456789abcdef" for c in body)
