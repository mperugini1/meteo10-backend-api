from datetime import datetime, timedelta
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from mysql.connector import errorcode
from mysql.connector import errors as mysql_errors

from app.db.pool import connection, execute, query_all
from app.deps import AdminUser, CurrentUser, Db
from app.errors import ApiError
from app.fmt import number_br
from app.schemas import StationCreate, StationUpdate
from app.services import readings
from app.timeutil import iso, to_local, to_naive_utc, utcnow

router = APIRouter(tags=["estações e medições"])

MAX_LIMIT = 1000
MAX_RAW_RANGE = timedelta(days=400)


def period(from_: datetime | None, to: datetime | None, default: timedelta = timedelta(hours=24)) -> tuple[datetime, datetime]:
    end = to_naive_utc(to) if to else utcnow()
    start = to_naive_utc(from_) if from_ else end - default
    if start >= end:
        raise ApiError(400, "invalid_period", "O início do período deve ser anterior ao fim.")
    if end - start > MAX_RAW_RANGE:
        raise ApiError(400, "period_too_long", "O período máximo é de 400 dias.")
    return start, end


# ---------------------------------------------------------------------------
# Estações
# ---------------------------------------------------------------------------

@router.get("/stations")
def list_stations(user: CurrentUser, conn: Db, include_inactive: bool = False) -> dict[str, Any]:
    sql = "SELECT * FROM stations"
    if not (include_inactive and user["role"] == "admin"):
        sql += " WHERE is_active = TRUE"
    stations = query_all(conn, sql + " ORDER BY id")
    now = utcnow()
    counts = readings.active_alarm_counts(conn)
    return {"items": [readings.serialize_station(conn, s, now, counts) for s in stations]}


@router.get("/stations/{station_id}")
def get_station(station_id: int, user: CurrentUser, conn: Db) -> dict[str, Any]:
    station = readings.get_station_or_404(conn, station_id)
    data = readings.serialize_station(conn, station, utcnow(), readings.active_alarm_counts(conn))
    data["last_measurement"] = readings.last_measurement_meta(conn, station_id)
    return data


@router.post("/stations", status_code=201)
def create_station(body: StationCreate, admin: AdminUser, conn: Db) -> dict[str, Any]:
    try:
        execute(
            conn,
            "INSERT INTO stations (id, name, description, latitude, longitude, altitude_m, measurement_interval_s, is_active)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (body.id, body.name, body.description, body.latitude, body.longitude, body.altitude_m,
             body.measurement_interval_s, body.is_active),
        )
        conn.commit()
    except mysql_errors.IntegrityError as exc:
        if exc.errno == errorcode.ER_DUP_ENTRY:
            raise ApiError(409, "station_exists", f"Já existe uma estação com o número {body.id}.") from exc
        raise
    return get_station(body.id, admin, conn)


@router.patch("/stations/{station_id}")
def update_station(station_id: int, body: StationUpdate, admin: AdminUser, conn: Db) -> dict[str, Any]:
    readings.get_station_or_404(conn, station_id)
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise ApiError(422, "validation_error", "O nome da estação é obrigatório.")
    if "measurement_interval_s" in changes and changes["measurement_interval_s"] is None:
        raise ApiError(422, "validation_error", "O intervalo de medição é obrigatório.")
    if "is_active" in changes and changes["is_active"] is None:
        raise ApiError(422, "validation_error", "Informe se a estação está ativa.")
    if changes:
        assignments = ", ".join(f"{column} = %s" for column in changes)
        execute(conn, f"UPDATE stations SET {assignments} WHERE id = %s", (*changes.values(), station_id))  # noqa: S608
        conn.commit()
    return get_station(station_id, admin, conn)


@router.delete("/stations/{station_id}")
def deactivate_station(station_id: int, admin: AdminUser, conn: Db) -> dict[str, Any]:
    readings.get_station_or_404(conn, station_id)
    execute(conn, "UPDATE stations SET is_active = FALSE WHERE id = %s", (station_id,))
    conn.commit()
    return get_station(station_id, admin, conn)


# ---------------------------------------------------------------------------
# Medições
# ---------------------------------------------------------------------------

@router.get("/stations/{station_id}/latest")
def latest(station_id: int, user: CurrentUser, conn: Db) -> dict[str, Any]:
    readings.get_station_or_404(conn, station_id)
    values = readings.latest_values(conn, station_id)
    return {
        "station_id": station_id,
        "measured_at": max((v["measured_at"] for v in values.values() if v), default=None),
        "values": values,
        "last_measurement": readings.last_measurement_meta(conn, station_id),
    }


@router.get("/stations/{station_id}/measurements")
def measurements(
    station_id: int,
    user: CurrentUser,
    conn: Db,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
) -> dict[str, Any]:
    readings.get_station_or_404(conn, station_id)
    start, end = period(from_, to)
    result = readings.list_measurements(conn, station_id, start, end, limit, cursor)
    return {"station_id": station_id, "from": iso(start), "to": iso(end), **result}


@router.get("/stations/{station_id}/measurements/aggregate")
def measurements_aggregate(
    station_id: int,
    user: CurrentUser,
    conn: Db,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    bucket: Literal["hour", "day"] = "hour",
) -> dict[str, Any]:
    readings.get_station_or_404(conn, station_id)
    start, end = period(from_, to, default=timedelta(days=7))
    return {
        "station_id": station_id,
        "from": iso(start),
        "to": iso(end),
        "bucket": bucket,
        "items": readings.aggregate(conn, station_id, start, end, bucket),
    }


CSV_COLUMNS = (
    ("Data e hora (horário de Brasília)", None),
    ("Horário estimado", None),
    ("Temperatura (°C)", ("temperature_c", 1)),
    ("Umidade do ar (%)", ("humidity_pct", 1)),
    ("Pressão (hPa)", ("pressure_hpa", 1)),
    ("Velocidade do vento (m/s)", ("wind_speed_ms", 1)),
    ("Direção do vento (°)", ("wind_direction_deg", 0)),
    ("Umidade do solo (%)", ("soil_moisture_pct", 0)),
    ("Índice UV", ("uv_index", 1)),
    ("Bateria (V)", ("battery_v", 2)),
    ("Sinal RSSI (dBm)", ("rssi_dbm", 0)),
    ("Relação sinal-ruído SNR (dB)", ("snr_db", 1)),
)


def _csv_line(cells: list[str]) -> str:
    def escape(cell: str) -> str:
        if any(ch in cell for ch in ';"\n'):
            return '"' + cell.replace('"', '""') + '"'
        return cell

    return ";".join(escape(c) for c in cells) + "\r\n"


def csv_rows(station_id: int, start: datetime, end: datetime) -> Iterator[str]:
    yield "﻿" + _csv_line([title for title, _ in CSV_COLUMNS])
    with connection() as conn:
        for row in readings.iter_measurements_asc(conn, station_id, start, end):
            cells = [to_local(row["measured_at"]).strftime("%d/%m/%Y %H:%M:%S"), "sim" if row["measured_at_estimated"] else "não"]
            for _, spec in CSV_COLUMNS[2:]:
                column, decimals = spec  # type: ignore[misc]
                cells.append(number_br(row[column], decimals))
            yield _csv_line(cells)


@router.get("/stations/{station_id}/measurements.csv")
def measurements_csv(
    station_id: int,
    user: CurrentUser,
    conn: Db,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
) -> StreamingResponse:
    readings.get_station_or_404(conn, station_id)
    start, end = period(from_, to)
    filename = f"meteo10_estacao{station_id}_{to_local(start):%Y%m%d}-{to_local(end):%Y%m%d}.csv"
    return StreamingResponse(
        csv_rows(station_id, start, end),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/stations/{station_id}/availability")
def station_availability(
    station_id: int,
    user: CurrentUser,
    conn: Db,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
) -> dict[str, Any]:
    station = readings.get_station_or_404(conn, station_id)
    start, end = period(from_, to)
    return {"station_id": station_id, **readings.availability_for(conn, station, start, end)}
