"""Argon2 password hashing.

Defaults: argon2id, memory=64MB, time=3, parallelism=4 (~250ms on modest CPU).
Single-purpose helpers — no business logic.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHash

_hasher = PasswordHasher()  # argon2id defaults

MIN_PASSWORD_LENGTH = 12


class WeakPassword(ValueError):
    pass


def validate_password(password: str) -> None:
    """Raise WeakPassword if too short. NIST 800-63B: length > complexity."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


def hash_password(password: str) -> str:
    validate_password(password)
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time verify. Returns False on mismatch / corruption / wrong type."""
    if not password_hash or not password:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when hash uses outdated argon2 params — re-hash on next login."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHash, VerificationError):
        return False
