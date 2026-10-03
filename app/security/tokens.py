"""Emissão e validação de JWT HS256 (PyJWT)."""

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.config import settings

ALGORITHM = "HS256"


def create_access_token(user_id: int, role: str, now: datetime | None = None) -> tuple[str, int]:
    """Retorna (token, expires_in em segundos)."""
    issued = now or datetime.now(timezone.utc)
    expires_in = settings.jwt_expires_minutes * 60
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(seconds=expires_in)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM), expires_in


def decode_access_token(token: str) -> dict[str, Any]:
    """Lança jwt.PyJWTError se o token for inválido ou expirado."""
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[ALGORITHM],
        options={"require": ["sub", "role", "exp", "iat"]},
    )
