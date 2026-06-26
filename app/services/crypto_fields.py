"""Encrypt sensitive HR fields at rest (Fernet)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import HR_ENCRYPTION_KEY, SECRET_KEY

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    if HR_ENCRYPTION_KEY:
        key = HR_ENCRYPTION_KEY.encode("ascii")
    else:
        digest = hashlib.sha256(f"hr-fields:{SECRET_KEY}".encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    _fernet = Fernet(key)
    return _fernet


def encrypt_field(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return _get_fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_field(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _get_fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Could not decrypt stored field.") from exc
