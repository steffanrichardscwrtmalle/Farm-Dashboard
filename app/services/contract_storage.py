"""Immutable signed contract PDF storage on local disk."""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from app.config import CONTRACTS_STORAGE_DIR


class ContractStorageError(Exception):
    """Signed PDF could not be stored or retrieved."""


def _safe_suffix(filename: str) -> str:
    """Return a lowercase, sanitised file extension (e.g. '.pdf')."""
    suffix = Path(filename or "").suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""


class LocalDiskStorage:
    def __init__(self, base_dir: str | None = None) -> None:
        self._base = Path(base_dir or CONTRACTS_STORAGE_DIR)
        self._signed_dir = self._base / "signed"
        self._documents_dir = self._base / "documents"

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

    # --- Staff documents (passport, licence, etc.) ---

    def save_document(
        self,
        *,
        employee_id: int,
        original_filename: str,
        content: bytes,
    ) -> tuple[str, str, int]:
        """Persist an uploaded document. Returns (stored_path, sha256, size)."""
        if not content:
            raise ContractStorageError("Empty file.")
        dest_dir = self._documents_dir / f"employee_{employee_id}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        sha256 = hashlib.sha256(content).hexdigest()
        filename = f"{uuid.uuid4().hex}{_safe_suffix(original_filename)}"
        path = dest_dir / filename
        path.write_bytes(content)
        return str(path), sha256, len(content)

    def resolve_document_path(self, stored_path: str) -> Path:
        path = Path(stored_path)
        if not path.is_file():
            raise ContractStorageError(f"Document not found: {stored_path}")
        return path

    def delete_document(self, stored_path: str) -> None:
        path = Path(stored_path)
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            raise ContractStorageError(f"Could not delete document: {exc}") from exc


default_storage = LocalDiskStorage()
