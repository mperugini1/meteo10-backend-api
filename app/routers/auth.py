from typing import Any

from fastapi import APIRouter, Request

from app.config import settings
from app.db.pool import execute, query_one
from app.deps import CurrentUser, Db
from app.errors import ApiError
from app.routers.serializers import user_out
from app.schemas import ChangePasswordRequest, LoginRequest
from app.security.passwords import hash_password, verify_password
from app.security.rate_limit import login_limiter
from app.security.tokens import create_access_token

router = APIRouter(prefix="/auth", tags=["autenticação"])

# Hash fictício para gastar o mesmo tempo quando o e-mail não existe (evita enumeração de usuários).
_DUMMY_SALT = "00" * 16


def client_ip(request: Request) -> str:
    direct = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and direct in settings.trusted_proxy_ips:
        return forwarded.split(",")[0].strip()
    return direct


@router.post("/login")
def login(body: LoginRequest, request: Request, conn: Db) -> dict[str, Any]:
    ip = client_ip(request)
    if login_limiter.is_blocked(ip):
        raise ApiError(429, "too_many_attempts", "Muitas tentativas de login. Aguarde alguns minutos e tente novamente.")
    user = query_one(conn, "SELECT * FROM users WHERE email = %s", (body.email,))
    if user is None:
        hash_password(body.password, _DUMMY_SALT)
        valid = False
    else:
        valid = verify_password(body.password, user["password_hash"], user["password_salt"])
    if not valid or not user["is_active"]:
        login_limiter.register_failure(ip)
        raise ApiError(401, "invalid_credentials", "E-mail ou senha incorretos.")
    token, expires_in = create_access_token(user["id"], user["role"])
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in, "user": user_out(user)}


@router.get("/me")
def me(user: CurrentUser) -> dict[str, Any]:
    return user_out(user)


@router.post("/change-password")
def change_password(body: ChangePasswordRequest, user: CurrentUser, conn: Db) -> dict[str, Any]:
    row = query_one(conn, "SELECT password_hash, password_salt FROM users WHERE id = %s", (user["id"],))
    if row is None or not verify_password(body.current_password, row["password_hash"], row["password_salt"]):
        raise ApiError(400, "invalid_current_password", "A senha atual está incorreta.")
    if body.current_password == body.new_password:
        raise ApiError(400, "same_password", "A nova senha deve ser diferente da atual.")
    password_hash, salt = hash_password(body.new_password)
    execute(conn, "UPDATE users SET password_hash = %s, password_salt = %s WHERE id = %s", (password_hash, salt, user["id"]))
    conn.commit()
    return {"status": "ok"}
