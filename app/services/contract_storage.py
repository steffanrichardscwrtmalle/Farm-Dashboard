"""Immutable signed contract PDF storage on local disk."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import CONTRACTS_STORAGE_DIR


class ContractStorageError(Exception):
    """Signed PDF could not be stored or retrieved."""


class LocalDiskStorage:
    def __init__(self, base_dir: str | None = None) -> None:
        self._base = Path(base_dir or CONTRACTS_STORAGE_DIR)
        self._signed_dir = self._base / "signed"

    def _ensure_dirs(self) -> None:
        self._signed_dir.mkdir(parents=True, exist_ok=True)

    def save_signed_pdf(
        self,
        *,
        employee_id: int,
        submission_id: int,
        content: bytes,
    ) -> tuple[str, str]:
        if not content:
            raise ContractStorageError("Empty PDF content.")
        self._ensure_dirs()
        sha256 = hashlib.sha256(content).hexdigest()
        filename = f"employee_{employee_id}_submission_{submission_id}.pdf"
        path = self._signed_dir / filename
        if path.exists():
            existing_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if existing_hash == sha256:
                return str(path), sha256
            raise ContractStorageError(
                f"Signed PDF already exists at {path} with different content."
            )
        path.write_bytes(content)
        return str(path), sha256

    def open_signed_pdf(self, stored_path: str) -> bytes:
        path = Path(stored_path)
        if not path.is_file():
            raise ContractStorageError(f"Signed PDF not found: {stored_path}")
        return path.read_bytes()

    def resolve_download_path(self, stored_path: str) -> Path:
        path = Path(stored_path)
        if not path.is_file():
            raise ContractStorageError(f"Signed PDF not found: {stored_path}")
        return path


default_storage = LocalDiskStorage()
