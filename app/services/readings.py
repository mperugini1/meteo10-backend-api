"""Consultas de estações e medições: status derivados, últimas leituras, agregação e disponibilidade."""

import base64
import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from mysql.connector.connection import MySQLConnection

from app.config import settings
from app.db.pool import query_all, query_one
from app.errors import ApiError
from app.services.processing import VALUE_COLUMNS, battery_pct
from app.timeutil import iso, local_offset_seconds, utcnow

NUMERIC_COLUMNS = tuple(c for c in VALUE_COLUMNS if c != "wind_direction_deg")
BUCKET_SECONDS = {"hour": 3600, "day": 86400}


def num(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


# ---------------------------------------------------------------------------
# Status derivados (seção 4.6)
# ---------------------------------------------------------------------------

def is_online(last_seen_at: datetime | None, interval_s: int, now: datetime, factor: int | None = None) -> bool:
    if last_seen_at is None:
        return False
    factor = settings.offline_factor if factor is None else factor
    return (now - last_seen_at).total_seconds() <= factor * interval_s


def availability(expected: int, received: int) -> float | None:
    if expected <= 0:
        return None
    return round(min(100.0, received / expected * 100), 1)


def expected_count(start: datetime, end: datetime, interval_s: int) -> int:
    if end <= start:
        return 0
    return int((end - start).total_seconds() // interval_s)


# ---------------------------------------------------------------------------
# Estações
# ---------------------------------------------------------------------------

def get_station_or_404(conn: MySQLConnection, station_id: int) -> dict[str, Any]:
    station = query_one(conn, "SELECT * FROM stations WHERE id = %s", (station_id,))
    if station is None:
        raise ApiError(404, "station_not_found", "Estação não encontrada.")
    return station


def latest_values(conn: MySQLConnection, station_id: int) -> dict[str, Any]:
    """Última medição válida de cada grandeza, cada uma com seu measured_at."""
    result: dict[str, Any] = {}
    for column in VALUE_COLUMNS:
        row = query_one(
            conn,
            f"SELECT {column} AS value, measured_at FROM measurements"  # noqa: S608 - coluna de lista fixa
            f" WHERE station_id = %s AND {column} IS NOT NULL ORDER BY measured_at DESC, id DESC LIMIT 1",
            (station_id,),
        )
        result[column] = {"value": num(row["value"]), "measured_at": iso(row["measured_at"])} if row else None
    return result


def last_measurement_meta(conn: MySQLConnection, station_id: int) -> dict[str, Any] | None:
    row = query_one(
        conn,
        "SELECT id, seq, flags, measured_at, received_at, rssi_dbm, snr_db, quality_flags FROM measurements"
        " WHERE station_id = %s ORDER BY measured_at DESC, id DESC LIMIT 1",
        (station_id,),
        json_columns=("quality_flags",),
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "seq": row["seq"],
        "flags": row["flags"],
        "measured_at": iso(row["measured_at"]),
        "received_at": iso(row["received_at"]),
        "rssi_dbm": row["rssi_dbm"],
        "snr_db": num(row["snr_db"]),
        "quality_flags": row["quality_flags"],
    }


def active_alarm_counts(conn: MySQLConnection) -> dict[int, int]:
    rows = query_all(conn, "SELECT station_id, COUNT(*) AS n FROM alarm_events WHERE resolved_at IS NULL GROUP BY station_id")
    return {r["station_id"]: r["n"] for r in rows}


def serialize_station(conn: MySQLConnection, station: dict[str, Any], now: datetime, alarm_counts: dict[int, int]) -> dict[str, Any]:
    battery_v = Decimal(station["last_battery_mv"]) / 1000 if station["last_battery_mv"] is not None else None
    latest = latest_values(conn, station["id"])
    return {
        "id": station["id"],
        "name": station["name"],
        "description": station["description"],
        "latitude": num(station["latitude"]),
        "longitude": num(station["longitude"]),
        "altitude_m": station["altitude_m"],
        "measurement_interval_s": station["measurement_interval_s"],
        "is_active": bool(station["is_active"]),
        "created_at": iso(station["created_at"]),
        "last_seen_at": iso(station["last_seen_at"]),
        "last_seq": station["last_seq"],
        "online": is_online(station["last_seen_at"], station["measurement_interval_s"], now),
        "offline_after_s": settings.offline_factor * station["measurement_interval_s"],
        "battery_v": num(battery_v),
        "battery_pct": battery_pct(battery_v),
        "active_alarms_count": alarm_counts.get(station["id"], 0),
        "latest": {column: (item["value"] if item else None) for column, item in latest.items()},
        "latest_measured_at": max((item["measured_at"] for item in latest.values() if item), default=None),
    }


# ---------------------------------------------------------------------------
# Medições
# ---------------------------------------------------------------------------

def serialize_measurement(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "station_id": row["station_id"],
        "seq": row["seq"],
        "flags": row["flags"],
        "measured_at": iso(row["measured_at"]),
        "measured_at_estimated": bool(row["measured_at_estimated"]),
        "received_at": iso(row["received_at"]),
        **{column: num(row[column]) for column in VALUE_COLUMNS},
        "rssi_dbm": row["rssi_dbm"],
        "snr_db": num(row["snr_db"]),
        "quality_flags": row["quality_flags"],
    }


def encode_cursor(measured_at: datetime, row_id: int) -> str:
    return base64.urlsafe_b64encode(f"{iso(measured_at)}|{row_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        text = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, row_id = text.split("|")
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ"), int(row_id)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(400, "invalid_cursor", "Cursor de paginação inválido.") from exc


def list_measurements(
    conn: MySQLConnection, station_id: int, start: datetime, end: datetime, limit: int, cursor: str | None
) -> dict[str, Any]:
    sql = "SELECT * FROM measurements WHERE station_id = %s AND measured_at >= %s AND measured_at < %s"
    params: list[Any] = [station_id, start, end]
    if cursor:
        c_time, c_id = decode_cursor(cursor)
        sql += " AND (measured_at < %s OR (measured_at = %s AND id < %s))"
        params += [c_time, c_time, c_id]
    sql += " ORDER BY measured_at DESC, id DESC LIMIT %s"
    params.append(limit + 1)
    rows = query_all(conn, sql, params, json_columns=("quality_flags",))
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1]["measured_at"], rows[-1]["id"]) if has_more and rows else None
    return {"items": [serialize_measurement(r) for r in rows], "next_cursor": next_cursor}


def iter_measurements_asc(conn: MySQLConnection, station_id: int, start: datetime, end: datetime, batch: int = 2000):
    """Percorre o período em ordem crescente, em lotes (exportação CSV)."""
    last_time, last_id = start, 0
    first = True
    while True:
        if first:
            sql = ("SELECT * FROM measurements WHERE station_id = %s AND measured_at >= %s AND measured_at < %s"
                   " ORDER BY measured_at, id LIMIT %s")
            params: list[Any] = [station_id, start, end, batch]
        else:
            sql = ("SELECT * FROM measurements WHERE station_id = %s AND measured_at < %s"
                   " AND (measured_at > %s OR (measured_at = %s AND id > %s)) ORDER BY measured_at, id LIMIT %s")
            params = [station_id, end, last_time, last_time, last_id, batch]
        rows = query_all(conn, sql, params)
        if not rows:
            return
        yield from rows
        last_time, last_id = rows[-1]["measured_at"], rows[-1]["id"]
        first = False
        if len(rows) < batch:
            return


def vector_mean_direction(sum_sin: float | None, sum_cos: float | None, count: int) -> int | None:
    """Média vetorial de direções (graus). None se não houver dados ou se a resultante for nula."""
    if not count or sum_sin is None or sum_cos is None:
        return None
    mean_sin, mean_cos = sum_sin / count, sum_cos / count
    if math.hypot(mean_sin, mean_cos) < 1e-6:
        return None
    return int(round(math.degrees(math.atan2(mean_sin, mean_cos)))) % 360


def aggregate(conn: MySQLConnection, station_id: int, start: datetime, end: datetime, bucket: str) -> list[dict[str, Any]]:
    """Agrega por hora ou por dia (dia civil de America/Sao_Paulo)."""
    size = BUCKET_SECONDS[bucket]
    offset = local_offset_seconds(start) if bucket == "day" else 0
    selects = []
    for column in NUMERIC_COLUMNS:
        selects += [
            f"MIN({column}) AS {column}__min",
            f"AVG({column}) AS {column}__avg",
            f"MAX({column}) AS {column}__max",
            f"COUNT({column}) AS {column}__count",
        ]
    selects += [
        "SUM(SIN(RADIANS(wind_direction_deg))) AS dir__sin",
        "SUM(COS(RADIANS(wind_direction_deg))) AS dir__cos",
        "COUNT(wind_direction_deg) AS dir__count",
    ]
    bucket_expr = f"(FLOOR((UNIX_TIMESTAMP(measured_at) + {offset}) / {size}) * {size} - {offset})"
    sql = (
        f"SELECT {bucket_expr} AS bucket_epoch, COUNT(*) AS count, {', '.join(selects)}"  # noqa: S608
        " FROM measurements WHERE station_id = %s AND measured_at >= %s AND measured_at < %s"
        " GROUP BY bucket_epoch ORDER BY bucket_epoch"
    )
    rows = query_all(conn, sql, (station_id, start, end))
    items = []
    for row in rows:
        item: dict[str, Any] = {
            "bucket_start": iso(datetime(1970, 1, 1) + timedelta(seconds=int(row["bucket_epoch"]))),
            "count": row["count"],
        }
        for column in NUMERIC_COLUMNS:
            count = row[f"{column}__count"]
            avg = row[f"{column}__avg"]
            item[column] = {
                "min": num(row[f"{column}__min"]),
                "avg": round(float(avg), 3) if avg is not None else None,
                "max": num(row[f"{column}__max"]),
                "count": count,
            }
        dir_count = row["dir__count"]
        item["wind_direction_deg"] = {
            "avg": vector_mean_direction(row["dir__sin"], row["dir__cos"], dir_count),
            "count": dir_count,
        }
        items.append(item)
    return items


def availability_for(conn: MySQLConnection, station: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    """Disponibilidade = recebidas / esperadas, limitando o período ao início da operação da estação."""
    now = utcnow()
    end = min(end, now)
    first = query_one(conn, "SELECT MIN(measured_at) AS first FROM measurements WHERE station_id = %s", (station["id"],))
    operation_start = station["created_at"]
    if first and first["first"] is not None:
        operation_start = min(operation_start, first["first"])
    effective_start = max(start, operation_start)
    expected = expected_count(effective_start, end, station["measurement_interval_s"])
    received = 0
    if end > effective_start:
        row = query_one(
            conn,
            "SELECT COUNT(*) AS n FROM measurements WHERE station_id = %s AND measured_at >= %s AND measured_at < %s",
            (station["id"], effective_start, end),
        )
        received = row["n"] if row else 0
    return {
        "from": iso(start),
        "to": iso(end),
        "effective_from": iso(effective_start) if end > effective_start else None,
        "expected": expected,
        "received": received,
        "availability_pct": availability(expected, received),
    }
