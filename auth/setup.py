"""First-run setup token bootstrap.

Called from app lifespan after init_auth_db. If the users table is empty:
  1. Reuse existing setup_token if `auth_setup` already has one.
  2. Otherwise generate a new 32-byte hex token, store it, log it, and
     write `data/setup_token.txt` for ops convenience.

Operator visits /setup with the token to create the first superadmin.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from auth.store import get_auth_store

logger = logging.getLogger(__name__)

_TOKEN_FILE = "data/setup_token.txt"


async def is_setup_required() -> bool:
    return await get_auth_store().count_users() == 0


async def ensure_setup_token() -> str | None:
    """If no users exist, ensure a setup token is present and return it.
    Returns None when the system is already set up.
    """
    store = get_auth_store()
    if await store.count_users() > 0:
        # Already set up — nothing to do. Clear any leftover token file.
        _delete_token_file()
        return None

    token = await store.get_setup_token()
    if not token:
        token = secrets.token_hex(32)
        await store.set_setup_token(token)

    logger.warning(
        "════════════════════════════════════════════════════════════════════\n"
        "  FIRST-RUN SETUP — no users in database.\n"
        "  Visit /setup with the token below to create the superadmin user:\n"
        "\n"
        "    setup token: %s\n"
        "\n"
        "  Token also written to %s\n"
        "════════════════════════════════════════════════════════════════════",
        token, _TOKEN_FILE,
    )
    _write_token_file(token)
    return token


def _write_token_file(token: str) -> None:
    try:
        path = Path(_TOKEN_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n")
        os.chmod(path, 0o600)
    except OSError as ex:
        logger.warning("Could not write %s: %s", _TOKEN_FILE, ex)


def _delete_token_file() -> None:
    try:
        Path(_TOKEN_FILE).unlink(missing_ok=True)
    except OSError:
        pass


async def consume_setup_token() -> None:
    await get_auth_store().consume_setup_token()
    _delete_token_file()
