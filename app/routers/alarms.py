from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query

from app.db.pool import execute, query_all, query_one
from app.deps import CurrentUser, Db
from app.errors import ApiError
from app.routers.serializers import event_out, rule_out
from app.schemas import AlarmRuleCreate, AlarmRuleUpdate
from app.services import alarms, readings
from app.timeutil import to_naive_utc, utcnow

router = APIRouter(tags=["alarmes"])


def _get_rule(conn, rule_id: int) -> dict[str, Any]:
    rule = query_one(conn, "SELECT * FROM alarm_rules WHERE id = %s", (rule_id,))
    if rule is None:
        raise ApiError(404, "rule_not_found", "Regra de alarme não encontrada.")
    return rule


@router.get("/stations/{station_id}/alarm-rules")
def list_rules(station_id: int, user: CurrentUser, conn: Db) -> dict[str, Any]:
    readings.get_station_or_404(conn, station_id)
    rules = query_all(conn, "SELECT * FROM alarm_rules WHERE station_id = %s ORDER BY id", (station_id,))
    return {"items": [rule_out(r) for r in rules]}


@router.post("/stations/{station_id}/alarm-rules", status_code=201)
def create_rule(station_id: int, body: AlarmRuleCreate, user: CurrentUser, conn: Db) -> dict[str, Any]:
    readings.get_station_or_404(conn, station_id)
    rule_id, _ = execute(
        conn,
        "INSERT INTO alarm_rules (station_id, variable, operator, threshold, hysteresis, is_enabled, created_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (station_id, body.variable, body.operator, body.threshold, body.hysteresis, body.is_enabled, user["id"]),
    )
    conn.commit()
    return rule_out(_get_rule(conn, rule_id))


@router.patch("/alarm-rules/{rule_id}")
def update_rule(rule_id: int, body: AlarmRuleUpdate, user: CurrentUser, conn: Db) -> dict[str, Any]:
    rule = _get_rule(conn, rule_id)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if changes:
        assignments = ", ".join(f"{column} = %s" for column in changes)
        execute(conn, f"UPDATE alarm_rules SET {assignments} WHERE id = %s", (*changes.values(), rule_id))  # noqa: S608
        # Ao desativar a regra ou mudar sua condição, o evento aberto deixa de fazer sentido.
        condition_changed = any(k in changes and changes[k] != rule[k] for k in ("variable", "operator", "threshold"))
        if changes.get("is_enabled") is False or condition_changed:
            alarms.resolve_rule_events(conn, rule_id, utcnow())
        conn.commit()
    return rule_out(_get_rule(conn, rule_id))


@router.delete("/alarm-rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, user: CurrentUser, conn: Db) -> None:
    _get_rule(conn, rule_id)
    alarms.resolve_rule_events(conn, rule_id, utcnow())
    execute(conn, "DELETE FROM alarm_rules WHERE id = %s", (rule_id,))
    conn.commit()


EVENT_SELECT = (
    "SELECT e.*, s.name AS station_name, u.name AS acknowledged_by_name FROM alarm_events e"
    " JOIN stations s ON s.id = e.station_id LEFT JOIN users u ON u.id = e.acknowledged_by"
)


@router.get("/alarm-events")
def list_events(
    user: CurrentUser,
    conn: Db,
    station_id: int | None = None,
    status: Literal["active", "resolved", "all"] = "all",
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(100, ge=1, le=1000),
    cursor: int | None = Query(None, ge=1, description="id do último evento da página anterior"),
) -> dict[str, Any]:
    where, params = [], []
    if station_id is not None:
        where.append("e.station_id = %s")
        params.append(station_id)
    if status == "active":
        where.append("e.resolved_at IS NULL")
    elif status == "resolved":
        where.append("e.resolved_at IS NOT NULL")
    if from_ is not None:
        where.append("e.started_at >= %s")
        params.append(to_naive_utc(from_))
    if to is not None:
        where.append("e.started_at < %s")
        params.append(to_naive_utc(to))
    if cursor is not None:
        where.append("e.id < %s")
        params.append(cursor)
    sql = EVENT_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY e.id DESC LIMIT %s"
    rows = query_all(conn, sql, (*params, limit + 1))
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [event_out(r) for r in rows], "next_cursor": str(rows[-1]["id"]) if has_more and rows else None}


@router.post("/alarm-events/{event_id}/acknowledge")
def acknowledge(event_id: int, user: CurrentUser, conn: Db) -> dict[str, Any]:
    event = query_one(conn, "SELECT id, acknowledged_at FROM alarm_events WHERE id = %s", (event_id,))
    if event is None:
        raise ApiError(404, "event_not_found", "Evento de alarme não encontrado.")
    if event["acknowledged_at"] is None:
        execute(
            conn,
            "UPDATE alarm_events SET acknowledged_at = %s, acknowledged_by = %s WHERE id = %s AND acknowledged_at IS NULL",
            (utcnow(), user["id"], event_id),
        )
        conn.commit()
    row = query_one(conn, EVENT_SELECT + " WHERE e.id = %s", (event_id,))
    return event_out(row)  # type: ignore[arg-type]
