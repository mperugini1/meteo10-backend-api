"""Ingestão de medições enviadas pelo gateway (seções 4.4 e 7.2)."""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping

from mysql.connector import errorcode
from mysql.connector import errors as mysql_errors
from mysql.connector.connection import MySQLConnection
from pydantic import ValidationError

from app.config import settings
from app.db.pool import execute, query_one
from app.errors import ApiError
from app.schemas import IngestPayload
from app.security import gateway_signature
from app.services import alarms
from app.services.processing import FLAG_RETRANSMITTED, convert, estimate_measured_at
from app.timeutil import to_naive_utc, utcnow

NONCE_RE = re.compile(r"^[0-9a-fA-F]{32}$")
DEDUP_WINDOW = timedelta(hours=24)


def _unauthorized(code: str, message: str) -> ApiError:
    return ApiError(401, code, message)


@dataclass
class IngestResult:
    status: str  # "created" | "duplicate"
    measurement_id: int | None = None
    opened_events: list[dict[str, Any]] = field(default_factory=list)


def authenticate_gateway(conn: MySQLConnection, headers: Mapping[str, str], body: bytes, now_epoch: float | None = None) -> int:
    """Valida cabeçalhos, janela de tempo, CMAC e nonce. Retorna o id do gateway."""
    gateway_id_h = headers.get("x-gateway-id", "")
    timestamp_h = headers.get("x-timestamp", "")
    nonce = headers.get("x-nonce", "")
    signature = headers.get("x-signature", "")
    if not (gateway_id_h.isdigit() and timestamp_h.lstrip("-").isdigit() and NONCE_RE.match(nonce) and signature):
        raise _unauthorized("invalid_signature_headers", "Cabeçalhos de assinatura ausentes ou inválidos.")

    gateway_id = int(gateway_id_h)
    gateway = query_one(conn, "SELECT id, secret_key, is_active FROM gateways WHERE id = %s", (gateway_id,))
    if gateway is None or not gateway["is_active"]:
        raise _unauthorized("invalid_gateway", "Gateway desconhecido ou inativo.")

    now = time.time() if now_epoch is None else now_epoch
    if abs(now - int(timestamp_h)) > settings.ingest_time_window_s:
        raise _unauthorized("timestamp_out_of_window", "Horário da requisição fora da janela permitida.")

    # A assinatura é conferida antes de gravar o nonce, para que requisições
    # não autenticadas não escrevam no banco.
    if not gateway_signature.verify(bytes(gateway["secret_key"]), gateway_id_h, timestamp_h, nonce, body, signature.lower()):
        raise _unauthorized("invalid_signature", "Assinatura inválida.")

    try:
        execute(conn, "INSERT INTO ingest_nonces (gateway_id, nonce) VALUES (%s, %s)", (gateway_id, nonce.lower()))
    except mysql_errors.IntegrityError as exc:
        conn.rollback()
        if exc.errno == errorcode.ER_DUP_ENTRY:
            raise _unauthorized("nonce_reused", "Requisição repetida (nonce já utilizado).") from exc
        raise
    execute(conn, "UPDATE gateways SET last_seen_at = %s WHERE id = %s", (utcnow(), gateway_id))
    conn.commit()
    return gateway_id


def parse_payload(body: bytes) -> tuple[IngestPayload, Any]:
    try:
        raw_json = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(422, "invalid_json", "Corpo da requisição não é um JSON válido.") from exc
    try:
        payload = IngestPayload.model_validate(raw_json)
    except ValidationError as exc:
        details = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]
        raise ApiError(422, "validation_error", "Dados inválidos. Verifique os campos informados.", details) from exc
    return payload, raw_json


def store_measurement(conn: MySQLConnection, gateway_id: int, payload: IngestPayload, raw_json: Any) -> IngestResult:
    """Processa e grava a medição em uma única transação (passos 2–7 da seção 4.4)."""
    received_at = to_naive_utc(payload.received_at) if payload.received_at else utcnow()

    # Bloqueia a linha da estação: serializa ingestões concorrentes da mesma estação (deduplicação segura).
    station = query_one(conn, "SELECT * FROM stations WHERE id = %s FOR UPDATE", (payload.station_id,))
    if station is None or not station["is_active"]:
        conn.rollback()
        raise ApiError(404, "station_not_found", "Estação não encontrada.")

    duplicate = query_one(
        conn,
        "SELECT id FROM measurements WHERE station_id = %s AND seq = %s AND received_at BETWEEN %s AND %s LIMIT 1",
        (payload.station_id, payload.seq, received_at - DEDUP_WINDOW, received_at + DEDUP_WINDOW),
    )
    if duplicate is not None:
        conn.rollback()
        return IngestResult(status="duplicate")

    processed = convert(payload.model_dump(), payload.flags)
    retransmitted = bool(payload.flags & FLAG_RETRANSMITTED)
    interval_s = station["measurement_interval_s"]
    if retransmitted:
        measured_at = estimate_measured_at(received_at, payload.seq, station["last_seq"], interval_s)
    else:
        measured_at = received_at

    values = processed.values
    measurement_id, _ = execute(
        conn,
        "INSERT INTO measurements (station_id, gateway_id, seq, flags, measured_at, measured_at_estimated, received_at,"
        " temperature_c, humidity_pct, pressure_hpa, wind_speed_ms, wind_direction_deg, soil_moisture_pct, uv_index,"
        " battery_v, rssi_dbm, snr_db, quality_flags, raw_payload)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            payload.station_id, gateway_id, payload.seq, payload.flags, measured_at, retransmitted, received_at,
            values["temperature_c"], values["humidity_pct"], values["pressure_hpa"], values["wind_speed_ms"],
            values["wind_direction_deg"], values["soil_moisture_pct"], values["uv_index"], values["battery_v"],
            payload.rssi_dbm, payload.snr_db,
            json.dumps(processed.quality_flags) if processed.quality_flags else None,
            json.dumps(raw_json, ensure_ascii=False),
        ),
    )

    # Só a medição mais recente define o estado atual da estação. Retransmissões (medições antigas)
    # apenas indicam que a estação está viva.
    previous_seen = station["last_seen_at"]
    fresh = previous_seen is None or received_at >= previous_seen
    current = fresh and not retransmitted
    if current:
        battery_mv = payload.battery_mv if values["battery_v"] is not None else station["last_battery_mv"]
        execute(
            conn,
            "UPDATE stations SET last_seen_at = %s, last_seq = %s, last_battery_mv = %s WHERE id = %s",
            (received_at, payload.seq, battery_mv, payload.station_id),
        )
    elif fresh:
        execute(conn, "UPDATE stations SET last_seen_at = %s WHERE id = %s", (received_at, payload.station_id))

    opened: list[dict[str, Any]] = []
    if fresh:
        alarms.resolve_offline(conn, payload.station_id, received_at)
    if current:
        opened = alarms.evaluate_measurement(conn, payload.station_id, measurement_id, values, measured_at)

    conn.commit()
    return IngestResult(status="created", measurement_id=measurement_id, opened_events=opened)


def cleanup_nonces(conn: MySQLConnection, now=None) -> int:
    now = now or utcnow()
    _, count = execute(
        conn, "DELETE FROM ingest_nonces WHERE created_at < %s", (now - timedelta(seconds=settings.nonce_retention_s),)
    )
    conn.commit()
    return count
