from __future__ import annotations

import secrets


def generate_api_key() -> str:
    """Return a URL-safe API key with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


__all__ = ["generate_api_key"]
