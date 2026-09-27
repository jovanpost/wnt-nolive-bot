"""Shared secret-prompt helpers for scripts you run BY HAND on your own machine.

Rule: nothing here ever prints a secret, ever echoes it back, and ever writes it to disk.
Values only ever live in this process's environment, for this one run.

Any script that needs a secret must call the matching need_*() function in here BEFORE
importing anything from `nolive` -- nolive.config reads secrets at import time, so if the
env var isn't set yet when that import happens, it'll just see it as blank.

    python3 scripts/verify_live_api.py
"""
from __future__ import annotations

import getpass
import os


def mask(value: str, keep: int = 4) -> str:
    """Safe to print: shows only the last `keep` characters, or all-stars if too short."""
    if not value:
        return "(empty)"
    value = str(value)
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def _prompt_hidden(label: str) -> str:
    return getpass.getpass("%s (hidden -- paste it and press Enter): " % label).strip()


def _prompt_multiline_hidden(label: str) -> str:
    print("%s" % label)
    print("Paste the FULL value (multi-line is fine), then on its own new line press Ctrl-D:")
    lines = []
    try:
        while True:
            lines.append(input())
    except EOFError:
        pass
    return "\n".join(lines).strip()


def need_env(name: str, label: str | None = None, multiline: bool = False) -> str:
    """Returns os.environ[name] if it's already set (e.g. from a real deployment's env).
    Otherwise prompts for it with hidden input, stores it in os.environ for this process
    only (nothing written to disk, nothing printed), and returns it."""
    existing = os.environ.get(name, "").strip()
    if existing:
        return existing
    value = _prompt_multiline_hidden(label or name) if multiline else _prompt_hidden(label or name)
    if value:
        os.environ[name] = value
    return value


def need_kalshi_credentials() -> tuple[str, str]:
    """Prompts for the two Kalshi live-trading secrets if they aren't already in the
    environment. Never prints either one."""
    key_id = need_env("KALSHI_KEY_ID", "Kalshi API key ID")
    pem = need_env(
        "KALSHI_PRIVATE_KEY_PEM",
        "Kalshi private key (the full -----BEGIN...----- to -----END...----- PEM block)",
        multiline=True,
    )
    return key_id, pem


def need_database_url() -> str:
    """Prompts for the Supabase DATABASE_URL if not already set. Leave blank to fall back
    to a local SQLite file instead (fine for a read-only market-data check)."""
    return need_env("DATABASE_URL", "Supabase DATABASE_URL (blank = use local SQLite instead)")
