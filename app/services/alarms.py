"""Avaliação de alarmes (seção 4.5).

- Regras do usuário: abertura quando a condição é verdadeira; fechamento considerando a histerese.
- Alarmes do sistema: bateria baixa/crítica, falha de sensor (3 pacotes seguidos) e estação offline.

Ponto de extensão para notificações (e-mail, push etc.): registre uma função com
`register_listener`; ela recebe cada evento aberto, após o commit da transação.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from mysql.connector.connection import MySQLConnection

from app.db.pool import execute, query_all, query_one
from app.fmt import VARIABLE_LABELS, number_br, with_unit
from app.services.processing import FLAG_RETRANSMITTED, SENSOR_LABELS, SENSORS

log = logging.getLogger(__name__)

BATTERY_LOW_MV = 3300
BATTERY_CRITICAL_MV = 3000
BATTERY_HYSTERESIS_MV = 100
SENSOR_FAULT_CONSECUTIVE = 3

OPERATOR_LABELS = {"gt": "acima de", "lt": "abaixo de"}

EventListener = Callable[[dict[str, Any]], None]
_listeners: list[EventListener] = []


def register_listener(listener: EventListener) -> None:
    _listeners.append(listener)


def notify_opened(events: list[dict[str, Any]]) -> None:
    for event in events:
        for listener in _listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - uma notificação com falha não pode derrubar a ingestão
                log.exception("Falha ao notificar evento de alarme %s", event.get("id"))


# ---------------------------------------------------------------------------
# Regras puras
# ---------------------------------------------------------------------------

def rule_triggered(operator: str, value: Decimal | float, threshold: Decimal | float) -> bool:
    return value > threshold if operator == "gt" else value < threshold


def rule_cleared(operator: str, value: Decimal | float, threshold: Decimal | float, hysteresis: Decimal | float) -> bool:
    """Valor voltou ao normal, com margem de histerese."""
    if operator == "gt":
        return value <= threshold - hysteresis
    return value >= threshold + hysteresis


def rule_description(variable: str, operator: str, threshold: Decimal | float) -> str:
    label = VARIABLE_LABELS[variable][0]
    return f"{label.capitalize()} {OPERATOR_LABELS[operator]} {with_unit(variable, threshold)}"


# ---------------------------------------------------------------------------
# Persistência de eventos
# ---------------------------------------------------------------------------

def _open_event(
    conn: MySQLConnection,
    *,
    station_id: int,
    type_: str,
    severity: str,
    message: str,
    started_at: datetime,
    rule_id: int | None = None,
    source: str | None = None,
    trigger_value: Decimal | float | None = None,
    measurement_id: int | None = None,
) -> dict[str, Any]:
    event_id, _ = execute(
        conn,
        "INSERT INTO alarm_events (station_id, rule_id, type, source, severity, message, trigger_value,"
        " measurement_id, started_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (station_id, rule_id, type_, source, severity, message[:300], trigger_value, measurement_id, started_at),
    )
    return {
        "id": event_id,
        "station_id": station_id,
        "rule_id": rule_id,
        "type": type_,
        "source": source,
        "severity": severity,
        "message": message,
        "trigger_value": trigger_value,
        "measurement_id": measurement_id,
        "started_at": started_at,
    }


def _resolve(conn: MySQLConnection, event_id: int, resolved_at: datetime) -> None:
    execute(conn, "UPDATE alarm_events SET resolved_at = %s WHERE id = %s AND resolved_at IS NULL", (resolved_at, event_id))


def _find_open(conn: MySQLConnection, station_id: int, type_: str, *, rule_id: int | None = None, source: str | None = None) -> dict[str, Any] | None:
    sql = "SELECT * FROM alarm_events WHERE station_id = %s AND type = %s AND resolved_at IS NULL"
    params: list[Any] = [station_id, type_]
    if rule_id is not None:
        sql += " AND rule_id = %s"
        params.append(rule_id)
    if source is not None:
        sql += " AND source = %s"
        params.append(source)
    return query_one(conn, sql + " ORDER BY id DESC LIMIT 1", params)


def resolve_rule_events(conn: MySQLConnection, rule_id: int, resolved_at: datetime) -> None:
    """Fecha eventos abertos de uma regra desativada ou removida."""
    execute(conn, "UPDATE alarm_events SET resolved_at = %s WHERE rule_id = %s AND resolved_at IS NULL", (resolved_at, rule_id))


# ---------------------------------------------------------------------------
# Avaliações
# ---------------------------------------------------------------------------

def evaluate_rules(conn: MySQLConnection, station_id: int, values: dict[str, Any], measurement_id: int, at: datetime) -> list[dict[str, Any]]:
    opened = []
    rules = query_all(conn, "SELECT * FROM alarm_rules WHERE station_id = %s AND is_enabled = TRUE", (station_id,))
    for rule in rules:
        value = values.get(rule["variable"])
        if value is None:
            continue  # grandeza em falha ou fora da faixa: mantém o estado atual
        value = Decimal(str(value))
        open_event = _find_open(conn, station_id, "rule", rule_id=rule["id"])
        if open_event is None:
            if rule_triggered(rule["operator"], value, rule["threshold"]):
                message = (
                    f"{rule_description(rule['variable'], rule['operator'], rule['threshold'])}"
                    f" (valor medido: {with_unit(rule['variable'], value)})"
                )
                opened.append(_open_event(
                    conn, station_id=station_id, type_="rule", severity="warning", message=message,
                    started_at=at, rule_id=rule["id"], trigger_value=value, measurement_id=measurement_id,
                ))
        elif rule_cleared(rule["operator"], value, rule["threshold"], rule["hysteresis"]):
            _resolve(conn, open_event["id"], at)
    return opened


def evaluate_battery(conn: MySQLConnection, station_id: int, battery_v: Decimal | None, measurement_id: int, at: datetime) -> list[dict[str, Any]]:
    if battery_v is None:
        return []
    mv = int(round(Decimal(str(battery_v)) * 1000))
    opened = []
    levels = (
        ("battery_low", "warning", BATTERY_LOW_MV, "Bateria baixa: {v}."),
        ("battery_critical", "critical", BATTERY_CRITICAL_MV, "Bateria crítica: {v}. A estação entra em hibernação para se proteger."),
    )
    for type_, severity, limit_mv, template in levels:
        open_event = _find_open(conn, station_id, type_)
        if open_event is None and mv < limit_mv:
            opened.append(_open_event(
                conn, station_id=station_id, type_=type_, severity=severity,
                message=template.format(v=f"{number_br(battery_v, 2)} V"),
                started_at=at, trigger_value=battery_v, measurement_id=measurement_id,
            ))
        elif open_event is not None and mv >= limit_mv + BATTERY_HYSTERESIS_MV:
            _resolve(conn, open_event["id"], at)
    return opened


def evaluate_sensor_faults(conn: MySQLConnection, station_id: int, measurement_id: int, at: datetime) -> list[dict[str, Any]]:
    """Abre evento quando o bit de falha aparece em 3 pacotes seguidos; fecha no primeiro sem falha."""
    recent = query_all(
        conn,
        "SELECT flags FROM measurements WHERE station_id = %s AND (flags & %s) = 0"
        " ORDER BY measured_at DESC, id DESC LIMIT %s",
        (station_id, FLAG_RETRANSMITTED, SENSOR_FAULT_CONSECUTIVE),
    )
    if not recent:
        return []
    opened = []
    for sensor, (bit, _) in SENSORS.items():
        open_event = _find_open(conn, station_id, "sensor_fault", source=sensor)
        current_fault = bool(recent[0]["flags"] & bit)
        if open_event is None:
            if len(recent) == SENSOR_FAULT_CONSECUTIVE and all(r["flags"] & bit for r in recent):
                opened.append(_open_event(
                    conn, station_id=station_id, type_="sensor_fault", severity="warning", source=sensor,
                    message=f"Falha no {SENSOR_LABELS[sensor]} em {SENSOR_FAULT_CONSECUTIVE} leituras seguidas.",
                    started_at=at, measurement_id=measurement_id,
                ))
        elif not current_fault:
            _resolve(conn, open_event["id"], at)
    return opened


def resolve_offline(conn: MySQLConnection, station_id: int, at: datetime) -> None:
    open_event = _find_open(conn, station_id, "station_offline")
    if open_event is not None:
        _resolve(conn, open_event["id"], at)


def evaluate_measurement(
    conn: MySQLConnection, station_id: int, measurement_id: int, values: dict[str, Any], at: datetime
) -> list[dict[str, Any]]:
    """Avaliação completa na ingestão de uma medição atual (não retransmitida)."""
    opened = []
    opened += evaluate_rules(conn, station_id, values, measurement_id, at)
    opened += evaluate_battery(conn, station_id, values.get("battery_v"), measurement_id, at)
    opened += evaluate_sensor_faults(conn, station_id, measurement_id, at)
    return opened


def offline_threshold_s(interval_s: int, factor: int) -> int:
    return interval_s * factor


def check_offline_stations(conn: MySQLConnection, now: datetime, factor: int) -> list[dict[str, Any]]:
    """Tarefa periódica: abre eventos de estação offline (seção 4.5)."""
    opened = []
    stations = query_all(
        conn,
        "SELECT id, measurement_interval_s, last_seen_at FROM stations WHERE is_active = TRUE AND last_seen_at IS NOT NULL",
    )
    for station in stations:
        limit_s = offline_threshold_s(station["measurement_interval_s"], factor)
        if now - station["last_seen_at"] <= timedelta(seconds=limit_s):
            continue
        if _find_open(conn, station["id"], "station_offline") is None:
            minutes = max(1, round(limit_s / 60))
            opened.append(_open_event(
                conn, station_id=station["id"], type_="station_offline", severity="critical",
                message=f"Estação sem comunicação há mais de {minutes} min.",
                started_at=now,
            ))
    return opened
