from __future__ import annotations

import hashlib
import secrets
import threading

import keyring
from keyring.errors import KeyringError

from app.config import get_settings

_memory_secrets: dict[str, str] = {}
_lock = threading.Lock()


def create_access_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, expected_hash: str) -> bool:
    return secrets.compare_digest(hash_token(token), expected_hash)


class SecretStore:
    """OS-keyring backed secrets with an in-process fallback for local demo mode."""

    def __init__(self) -> None:
        self.service = get_settings().secret_service_name

    def set(self, name: str, value: str) -> None:
        try:
            keyring.set_password(self.service, name, value)
            return
        except (KeyringError, RuntimeError, ValueError):
            with _lock:
                _memory_secrets[name] = value

    def get(self, name: str) -> str | None:
        try:
            value = keyring.get_password(self.service, name)
            if value is not None:
                return value
        except (KeyringError, RuntimeError, ValueError):
            pass
        with _lock:
            return _memory_secrets.get(name)

    def delete(self, name: str) -> None:
        try:
            keyring.delete_password(self.service, name)
        except (KeyringError, RuntimeError, ValueError):
            pass
        with _lock:
            _memory_secrets.pop(name, None)


secret_store = SecretStore()

