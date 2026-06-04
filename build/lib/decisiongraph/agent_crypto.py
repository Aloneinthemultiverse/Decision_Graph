"""Phase 2 (slice 3) — At-rest encryption for the agent grant/audit files.

Threat addressed: stolen disk / raw filesystem access. NOT a replacement for
the sandbox (runtime) or isolation (cross-tenant) — a different attacker
(see DOCUMENTATION.md threat table).

Uses Fernet (AES-128-CBC + HMAC-SHA256, authenticated) from the `cryptography`
library. A single key lives in a 0600-ish key file OUTSIDE the workspace
directories (storage/.dg_at_rest.key, gitignored) so a leaked workspace
folder alone is useless.

Design notes:
  * Grants file: encrypt the whole JSON blob (atomic replace).
  * Audit log: encrypt PER LINE so the file stays append-only (each record is
    an independent ciphertext token, one per line). Tamper/truncation of any
    line is detectable (Fernet is authenticated) without breaking the rest.
  * Opt-in: AgentAccessStore(encrypt=True). Default path is unchanged so all
    existing behaviour/tests are unaffected.
"""
from __future__ import annotations

import os
from cryptography.fernet import Fernet, InvalidToken

_KEY_ENV = "DG_AT_REST_KEY"


def _default_key_path() -> str:
    from . import config
    return os.path.join(config.STORAGE_DIR, ".dg_at_rest.key")


def load_or_create_key(path: str | None = None) -> bytes:
    """Return the at-rest key. Precedence: env var > key file > freshly
    generated key file. The key is never written into a workspace dir."""
    env = os.environ.get(_KEY_ENV)
    if env:
        return env.encode() if isinstance(env, str) else env
    path = path or _default_key_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(key)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)  # best effort on Windows
    except OSError:
        pass
    return key


class FileEncryptor:
    """Thin wrapper: encrypt/decrypt bytes and per-line text."""

    def __init__(self, key: bytes | None = None, key_path: str | None = None):
        self._f = Fernet(key or load_or_create_key(key_path))

    # whole-blob (grants json) -------------------------------------------
    def encrypt_text(self, text: str) -> bytes:
        return self._f.encrypt(text.encode("utf-8"))

    def decrypt_text(self, blob: bytes) -> str:
        return self._f.decrypt(blob).decode("utf-8")

    # per-line (audit jsonl) ---------------------------------------------
    def encrypt_line(self, line: str) -> str:
        return self._f.encrypt(line.encode("utf-8")).decode("ascii")

    def decrypt_line(self, token: str) -> str:
        return self._f.decrypt(token.encode("ascii")).decode("utf-8")

    def is_encrypted(self, data: bytes) -> bool:
        try:
            self._f.decrypt(data)
            return True
        except InvalidToken:
            return False
