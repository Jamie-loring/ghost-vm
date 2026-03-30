"""
creds.py — encrypted credential store for the ghost browser-vm

Credentials are encrypted at rest using Fernet symmetric encryption.
The key is derived from the API_KEY env var so the store is only
readable by someone who already has API access to the container.

Storage: /app/.creds.enc  (mounted volume — survives container restarts)
"""

import base64
import hashlib
import json
from pathlib import Path
from typing import Optional

import pyotp
from cryptography.fernet import Fernet, InvalidToken


CREDS_FILE = Path("/home/user/.config/chromium/.ghost_creds.enc")


def _derive_key(api_key: str) -> bytes:
    """Derive a 32-byte Fernet key from the API key via SHA-256."""
    digest = hashlib.sha256(api_key.encode()).digest()
    return base64.urlsafe_b64encode(digest)


class CredentialManager:
    """
    Encrypted key-value store mapping service names to credentials.

    Each entry holds:
        username    — login identifier (email or username)
        password    — plaintext password (encrypted at rest)
        totp_secret — optional base32 TOTP seed for 2FA
        notes       — optional freeform string
    """

    def __init__(self, api_key: str):
        self._fernet = Fernet(_derive_key(api_key))
        self._store: dict = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if not CREDS_FILE.exists():
            return {}
        try:
            return json.loads(self._fernet.decrypt(CREDS_FILE.read_bytes()))
        except (InvalidToken, Exception):
            return {}

    def _save(self) -> None:
        CREDS_FILE.write_bytes(self._fernet.encrypt(json.dumps(self._store).encode()))
        CREDS_FILE.chmod(0o600)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def store(
        self,
        service: str,
        username: str,
        password: str,
        totp_secret: Optional[str] = None,
        notes: str = "",
    ) -> None:
        """Store or overwrite credentials for a service."""
        if totp_secret:
            # Validate the secret is usable before storing
            try:
                pyotp.TOTP(totp_secret).now()
            except Exception as e:
                raise ValueError(f"Invalid TOTP secret: {e}")
        self._store[service] = {
            "username": username,
            "password": password,
            "totp_secret": totp_secret,
            "notes": notes,
        }
        self._save()

    def get(self, service: str) -> Optional[dict]:
        return self._store.get(service)

    def delete(self, service: str) -> bool:
        if service in self._store:
            del self._store[service]
            self._save()
            return True
        return False

    def list_services(self) -> list:
        """Return service names with metadata but never passwords."""
        return [
            {
                "service": s,
                "username": v["username"],
                "has_totp": bool(v.get("totp_secret")),
                "notes": v.get("notes", ""),
            }
            for s, v in self._store.items()
        ]

    # ------------------------------------------------------------------
    # TOTP
    # ------------------------------------------------------------------

    def get_totp(self, service: str) -> Optional[str]:
        """Return the current 6-digit TOTP code for a service, or None."""
        entry = self._store.get(service)
        if not entry or not entry.get("totp_secret"):
            return None
        return pyotp.TOTP(entry["totp_secret"]).now()
