import secrets
from typing import Any

from fastapi import APIRouter
from mysql.connector import errorcode
from mysql.connector import errors as mysql_errors

from app.db.pool import execute, query_all, query_one
from app.deps import AdminUser, Db
from app.errors import ApiError
from app.routers.serializers import gateway_out, user_out
from app.schemas import GatewayCreate, GatewayUpdate, UserCreate, UserUpdate
from app.security.passwords import hash_password

router = APIRouter(tags=["administração"])

GATEWAY_COLUMNS = "id, name, is_active, last_seen_at, created_at"
USER_COLUMNS = "id, name, email, role, is_active, created_at"


# ---------------------------------------------------------------------------
# Gateways
# ---------------------------------------------------------------------------

def _get_gateway(conn, gateway_id: int) -> dict[str, Any]:
    row = query_one(conn, f"SELECT {GATEWAY_COLUMNS} FROM gateways WHERE id = %s", (gateway_id,))  # noqa: S608
    if row is None:
        raise ApiError(404, "gateway_not_found", "Gateway não encontrado.")
    return row


def new_secret() -> bytes:
    return secrets.token_bytes(16)


@router.get("/gateways")
def list_gateways(admin: AdminUser, conn: Db) -> dict[str, Any]:
    rows = query_all(conn, f"SELECT {GATEWAY_COLUMNS} FROM gateways ORDER BY id")  # noqa: S608
    return {"items": [gateway_out(r) for r in rows]}


@router.post("/gateways", status_code=201)
def create_gateway(body: GatewayCreate, admin: AdminUser, conn: Db) -> dict[str, Any]:
    secret = new_secret()
    gateway_id, _ = execute(
        conn, "INSERT INTO gateways (name, secret_key, is_active) VALUES (%s, %s, %s)", (body.name, secret, body.is_active)
    )
    conn.commit()
    return {**gateway_out(_get_gateway(conn, gateway_id)), "secret_hex": secret.hex()}


@router.post("/gateways/{gateway_id}/rotate-secret")
def rotate_secret(gateway_id: int, admin: AdminUser, conn: Db) -> dict[str, Any]:
    _get_gateway(conn, gateway_id)
    secret = new_secret()
    execute(conn, "UPDATE gateways SET secret_key = %s WHERE id = %s", (secret, gateway_id))
    conn.commit()
    return {**gateway_out(_get_gateway(conn, gateway_id)), "secret_hex": secret.hex()}


@router.patch("/gateways/{gateway_id}")
def update_gateway(gateway_id: int, body: GatewayUpdate, admin: AdminUser, conn: Db) -> dict[str, Any]:
    _get_gateway(conn, gateway_id)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if changes:
        assignments = ", ".join(f"{column} = %s" for column in changes)
        execute(conn, f"UPDATE gateways SET {assignments} WHERE id = %s", (*changes.values(), gateway_id))  # noqa: S608
        conn.commit()
    return gateway_out(_get_gateway(conn, gateway_id))


# ---------------------------------------------------------------------------
# Usuários (sem exclusão física: desativar com is_active = false)
# ---------------------------------------------------------------------------

def _get_user(conn, user_id: int) -> dict[str, Any]:
    row = query_one(conn, f"SELECT {USER_COLUMNS} FROM users WHERE id = %s", (user_id,))  # noqa: S608
    if row is None:
        raise ApiError(404, "user_not_found", "Usuário não encontrado.")
    return row


def _email_conflict(exc: mysql_errors.IntegrityError) -> ApiError:
    if exc.errno == errorcode.ER_DUP_ENTRY:
        return ApiError(409, "email_in_use", "Já existe um usuário com este e-mail.")
    raise exc


@router.get("/users")
def list_users(admin: AdminUser, conn: Db) -> dict[str, Any]:
    rows = query_all(conn, f"SELECT {USER_COLUMNS} FROM users ORDER BY name")  # noqa: S608
    return {"items": [user_out(r) for r in rows]}


@router.get("/users/{user_id}")
def get_user(user_id: int, admin: AdminUser, conn: Db) -> dict[str, Any]:
    return user_out(_get_user(conn, user_id))


@router.post("/users", status_code=201)
def create_user(body: UserCreate, admin: AdminUser, conn: Db) -> dict[str, Any]:
    password_hash, salt = hash_password(body.password)
    try:
        user_id, _ = execute(
            conn,
            "INSERT INTO users (name, email, password_hash, password_salt, role, is_active) VALUES (%s, %s, %s, %s, %s, %s)",
            (body.name, body.email, password_hash, salt, body.role, body.is_active),
        )
        conn.commit()
    except mysql_errors.IntegrityError as exc:
        raise _email_conflict(exc) from exc
    return user_out(_get_user(conn, user_id))


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserUpdate, admin: AdminUser, conn: Db) -> dict[str, Any]:
    _get_user(conn, user_id)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if user_id == admin["id"] and (changes.get("role") == "viewer" or changes.get("is_active") is False):
        raise ApiError(400, "cannot_demote_self", "Você não pode remover seu próprio acesso de administrador.")
    password = changes.pop("password", None)
    if password is not None:
        changes["password_hash"], changes["password_salt"] = hash_password(password)
    if changes:
        assignments = ", ".join(f"{column} = %s" for column in changes)
        try:
            execute(conn, f"UPDATE users SET {assignments} WHERE id = %s", (*changes.values(), user_id))  # noqa: S608
            conn.commit()
        except mysql_errors.IntegrityError as exc:
            raise _email_conflict(exc) from exc
    return user_out(_get_user(conn, user_id))
