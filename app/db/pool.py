"""Pool de conexões MySQL e funções auxiliares para SQL puro (sem ORM)."""

import json
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from mysql.connector import pooling
from mysql.connector.connection import MySQLConnection

from app.config import settings

_pool: pooling.MySQLConnectionPool | None = None
_pool_lock = threading.Lock()
# O pool do mysql-connector falha imediatamente quando esgotado; o semáforo faz as threads aguardarem.
_slots: threading.BoundedSemaphore | None = None

ACQUIRE_TIMEOUT_S = 15


class DatabaseUnavailable(RuntimeError):
    pass


def _connect_args(database: str | None = None) -> dict[str, Any]:
    return {
        "host": settings.db_host,
        "port": settings.db_port,
        "user": settings.db_user,
        "password": settings.db_password,
        "database": database if database is not None else settings.db_name,
        "charset": "utf8mb4",
        "collation": "utf8mb4_0900_ai_ci",
        "time_zone": "+00:00",
        "use_pure": True,
    }


def get_pool() -> pooling.MySQLConnectionPool:
    global _pool, _slots
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = pooling.MySQLConnectionPool(
                    pool_name="meteo10",
                    pool_size=settings.db_pool_size,
                    pool_reset_session=True,
                    **_connect_args(),
                )
                _slots = threading.BoundedSemaphore(settings.db_pool_size)
    return _pool


def reset_pool() -> None:
    """Descarta o pool atual (usado pelos testes ao trocar de banco)."""
    global _pool, _slots
    with _pool_lock:
        _pool = None
        _slots = None


@contextmanager
def connection() -> Iterator[MySQLConnection]:
    pool = get_pool()
    assert _slots is not None
    slots = _slots
    if not slots.acquire(timeout=ACQUIRE_TIMEOUT_S):
        raise DatabaseUnavailable("Tempo esgotado aguardando conexão com o banco.")
    conn = None
    try:
        conn = pool.get_connection()
        # O reset de sessão do pool apaga variáveis de sessão; garantimos UTC a cada uso.
        cur = conn.cursor()
        cur.execute("SET time_zone = '+00:00'")
        cur.close()
        yield conn
    finally:
        if conn is not None:
            try:
                if conn.in_transaction:
                    conn.rollback()
            finally:
                conn.close()
        slots.release()


def raw_connection(database: str | None = None) -> MySQLConnection:
    """Conexão avulsa fora do pool (migrações, criação de banco de teste)."""
    import mysql.connector

    return mysql.connector.connect(**_connect_args(database))


def _decode_row(row: dict[str, Any] | None, json_columns: Sequence[str]) -> dict[str, Any] | None:
    if row is None:
        return None
    for col in json_columns:
        value = row.get(col)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        if isinstance(value, str):
            row[col] = json.loads(value)
    return row


def query_all(conn: MySQLConnection, sql: str, params: Sequence[Any] | dict = (), json_columns: Sequence[str] = ()) -> list[dict[str, Any]]:
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(sql, params)
        return [_decode_row(r, json_columns) for r in cur.fetchall()]  # type: ignore[misc]
    finally:
        cur.close()


def query_one(conn: MySQLConnection, sql: str, params: Sequence[Any] | dict = (), json_columns: Sequence[str] = ()) -> dict[str, Any] | None:
    rows = query_all(conn, sql, params, json_columns)
    return rows[0] if rows else None


def execute(conn: MySQLConnection, sql: str, params: Sequence[Any] | dict = ()) -> tuple[int, int]:
    """Executa um comando e retorna (lastrowid, rowcount)."""
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return cur.lastrowid or 0, cur.rowcount
    finally:
        cur.close()
