"""Dependências do FastAPI: conexão com o banco e usuário autenticado."""

from typing import Annotated, Any, Iterator

import jwt
from fastapi import Depends, Request
from mysql.connector.connection import MySQLConnection

from app.db.pool import connection, query_one
from app.errors import ApiError
from app.security.tokens import decode_access_token


def get_db() -> Iterator[MySQLConnection]:
    with connection() as conn:
        yield conn


Db = Annotated[MySQLConnection, Depends(get_db)]


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(401, "not_authenticated", "Faça login para continuar.")
    return token.strip()


def get_current_user(request: Request, conn: Db) -> dict[str, Any]:
    try:
        claims = decode_access_token(_bearer_token(request))
        user_id = int(claims["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise ApiError(401, "invalid_token", "Sessão expirada ou inválida. Faça login novamente.") from exc
    user = query_one(conn, "SELECT id, name, email, role, is_active, created_at FROM users WHERE id = %s", (user_id,))
    if user is None or not user["is_active"]:
        raise ApiError(401, "invalid_token", "Sessão expirada ou inválida. Faça login novamente.")
    return user


CurrentUser = Annotated[dict[str, Any], Depends(get_current_user)]


def require_admin(user: CurrentUser) -> dict[str, Any]:
    if user["role"] != "admin":
        raise ApiError(403, "forbidden", "Apenas administradores podem realizar esta ação.")
    return user


AdminUser = Annotated[dict[str, Any], Depends(require_admin)]
