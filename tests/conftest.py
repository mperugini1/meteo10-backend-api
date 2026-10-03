"""Fixtures do pytest.

Os testes de API usam o banco MySQL definido por TEST_DB_NAME (padrão: meteo10_test), recriado a cada sessão.
Se o banco não estiver acessível, esses testes são pulados; os testes unitários rodam sempre.
"""

import os

os.environ["DB_NAME"] = os.environ.get("TEST_DB_NAME", "meteo10_test")
os.environ["PBKDF2_ITERATIONS"] = "1000"
os.environ["BACKGROUND_TASKS"] = "false"
os.environ["SENTRY_DSN"] = ""
os.environ["JWT_SECRET"] = "segredo-de-teste-com-pelo-menos-32-bytes"

import json  # noqa: E402
import secrets  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

from app.db import pool  # noqa: E402
from app.db.migrate import migrate  # noqa: E402
from app.security.gateway_signature import sign  # noqa: E402
from app.security.passwords import hash_password  # noqa: E402
from app.security.rate_limit import login_limiter  # noqa: E402

TABLES = ("alarm_events", "alarm_rules", "measurements", "ingest_nonces", "stations", "gateways", "users")
ADMIN = {"email": "admin@meteo10.local", "password": "senha-admin-123"}
VIEWER = {"email": "viewer@meteo10.local", "password": "senha-viewer-123"}
GATEWAY_SECRET = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


def _db_available() -> bool:
    try:
        conn = pool.raw_connection()
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def database():
    if not _db_available():
        pytest.skip(f"Banco de teste '{os.environ['DB_NAME']}' indisponível")
    conn = pool.raw_connection()
    cur = conn.cursor()
    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
    for table in (*TABLES, "schema_migrations"):
        cur.execute(f"DROP TABLE IF EXISTS {table}")
    cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    conn.commit()
    conn.close()
    migrate(verbose=False)
    pool.reset_pool()
    yield


@pytest.fixture()
def db(database):
    """Banco limpo com admin, viewer, gateway 1 e estação 1."""
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in TABLES:
            cur.execute(f"TRUNCATE TABLE {table}")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        for user, role in ((ADMIN, "admin"), (VIEWER, "viewer")):
            h, s = hash_password(user["password"])
            cur.execute(
                "INSERT INTO users (name, email, password_hash, password_salt, role) VALUES (%s, %s, %s, %s, %s)",
                (role.capitalize(), user["email"], h, s, role),
            )
        cur.execute("INSERT INTO gateways (id, name, secret_key) VALUES (1, 'Gateway teste', %s)", (GATEWAY_SECRET,))
        cur.execute(
            "INSERT INTO stations (id, name, measurement_interval_s, created_at) VALUES (1, 'Estação Poli', 600, %s)",
            (datetime(2020, 1, 1),),
        )
        conn.commit()
        cur.close()
    login_limiter.reset()
    yield


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _token(client, user) -> str:
    response = client.post("/api/v1/auth/login", json=user)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture()
def admin_headers(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(client, ADMIN)}"}


@pytest.fixture()
def viewer_headers(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(client, VIEWER)}"}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def make_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "station_id": 1,
        "seq": 1534,
        "flags": 0,
        "temperature_raw": 2357,
        "humidity_raw": 6420,
        "pressure_raw": 9235,
        "wind_speed_raw": 312,
        "wind_direction_raw": 135,
        "soil_moisture_raw": 41,
        "uv_index_raw": 63,
        "battery_mv": 3987,
        "rssi_dbm": -97,
        "snr_db": 6.5,
        "received_at": iso(datetime.now(timezone.utc)),
    }
    payload.update(overrides)
    if isinstance(payload.get("received_at"), datetime):
        payload["received_at"] = iso(payload["received_at"])
    return payload


def signed_headers(body: bytes, gateway_id: int = 1, secret: bytes = GATEWAY_SECRET, timestamp: int | None = None, nonce: str | None = None) -> dict[str, str]:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or secrets.token_hex(16)
    return {
        "Content-Type": "application/json",
        "X-Gateway-Id": str(gateway_id),
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": sign(secret, gateway_id, ts, nonce, body),
    }


class Sender:
    def __init__(self, client):
        self.client = client

    def __call__(self, **overrides: Any):
        body = json.dumps(make_payload(**overrides)).encode()
        return self.client.post("/api/v1/ingest/measurements", content=body, headers=signed_headers(body))


@pytest.fixture()
def send(client) -> Sender:
    return Sender(client)


@pytest.fixture()
def base_time() -> datetime:
    """Instante recente, alinhado ao minuto, para séries de medições."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now - timedelta(hours=2)
