"""Hash de senhas com PBKDF2-HMAC-SHA256 e salt próprio por usuário (seção 7.1)."""

import hashlib
import hmac
import secrets

from app.config import settings

SALT_BYTES = 16


def hash_password(password: str, salt_hex: str | None = None, iterations: int | None = None) -> tuple[str, str]:
    """Retorna (hash_hex, salt_hex)."""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations or settings.pbkdf2_iterations
    )
    return digest.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str, iterations: int | None = None) -> bool:
    candidate, _ = hash_password(password, salt_hex, iterations)
    return hmac.compare_digest(candidate, hash_hex)
