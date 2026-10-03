"""Dados iniciais: admin, gateway de demonstração, Estação Poli e regras de geada/calor.

Uso: python -m app.db.seed   (idempotente)
"""

import secrets
import sys
from decimal import Decimal

from app.config import settings
from app.db.pool import connection, execute, query_one
from app.security.passwords import hash_password

DEMO_GATEWAY_NAME = "Gateway de demonstração"
DEMO_STATION = {
    "id": 1,
    "name": "Estação Poli",
    "description": "Estação de demonstração no campus da Escola Politécnica da USP.",
    "latitude": Decimal("-23.555800"),
    "longitude": Decimal("-46.731200"),
    "altitude_m": 760,
    "measurement_interval_s": 600,
}
# Limites agrícolas sugeridos (ponto em aberto 4): geada e calor
DEFAULT_RULES = (
    ("temperature_c", "lt", Decimal("2.00"), Decimal("1.00")),
    ("temperature_c", "gt", Decimal("35.00"), Decimal("1.00")),
)


def seed() -> None:
    with connection() as conn:
        admin = query_one(conn, "SELECT id FROM users WHERE email = %s", (settings.seed_admin_email.lower(),))
        if admin is None:
            password_hash, salt = hash_password(settings.seed_admin_password)
            admin_id, _ = execute(
                conn,
                "INSERT INTO users (name, email, password_hash, password_salt, role) VALUES (%s, %s, %s, %s, 'admin')",
                ("Administrador", settings.seed_admin_email.lower(), password_hash, salt),
            )
            print(f"Usuário admin criado: {settings.seed_admin_email}")
        else:
            admin_id = admin["id"]
            print(f"Usuário admin já existe: {settings.seed_admin_email}")

        gateway = query_one(conn, "SELECT id FROM gateways WHERE name = %s", (DEMO_GATEWAY_NAME,))
        if gateway is None:
            secret = bytes.fromhex(settings.seed_gateway_secret_hex) if settings.seed_gateway_secret_hex else secrets.token_bytes(16)
            if len(secret) != 16:
                raise ValueError("SEED_GATEWAY_SECRET_HEX deve ter 32 caracteres hexadecimais.")
            gateway_id, _ = execute(conn, "INSERT INTO gateways (name, secret_key) VALUES (%s, %s)", (DEMO_GATEWAY_NAME, secret))
            print(f"Gateway de demonstração criado: id={gateway_id}")
            print(f"  Segredo (hex, exibido apenas agora): {secret.hex()}")
        else:
            print(f"Gateway de demonstração já existe: id={gateway['id']} (use 'Gerar novo segredo' na administração se perdeu o segredo)")

        station = query_one(conn, "SELECT id FROM stations WHERE id = %s", (DEMO_STATION["id"],))
        if station is None:
            columns = ", ".join(DEMO_STATION)
            placeholders = ", ".join(["%s"] * len(DEMO_STATION))
            execute(conn, f"INSERT INTO stations ({columns}) VALUES ({placeholders})", tuple(DEMO_STATION.values()))  # noqa: S608
            for variable, operator, threshold, hysteresis in DEFAULT_RULES:
                execute(
                    conn,
                    "INSERT INTO alarm_rules (station_id, variable, operator, threshold, hysteresis, created_by)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (DEMO_STATION["id"], variable, operator, threshold, hysteresis, admin_id),
                )
            print("Estação Poli criada (id 1, intervalo 600 s) com regras de geada (< 2 °C) e calor (> 35 °C).")
        else:
            print("Estação Poli já existe.")
        conn.commit()


if __name__ == "__main__":
    try:
        seed()
    except Exception as exc:  # noqa: BLE001
        print(f"Erro no seed: {exc}", file=sys.stderr)
        sys.exit(1)
