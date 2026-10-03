"""Serialização das linhas do banco para JSON da API."""

from typing import Any

from app.services.readings import num
from app.timeutil import iso


def user_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "created_at": iso(row.get("created_at")),
    }


def gateway_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "is_active": bool(row["is_active"]),
        "last_seen_at": iso(row["last_seen_at"]),
        "created_at": iso(row["created_at"]),
    }


def rule_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "station_id": row["station_id"],
        "variable": row["variable"],
        "operator": row["operator"],
        "threshold": num(row["threshold"]),
        "hysteresis": num(row["hysteresis"]),
        "is_enabled": bool(row["is_enabled"]),
        "created_by": row["created_by"],
        "created_at": iso(row["created_at"]),
    }


def event_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "station_id": row["station_id"],
        "station_name": row.get("station_name"),
        "rule_id": row["rule_id"],
        "type": row["type"],
        "source": row.get("source"),
        "severity": row["severity"],
        "status": "active" if row["resolved_at"] is None else "resolved",
        "message": row["message"],
        "trigger_value": num(row["trigger_value"]),
        "measurement_id": row["measurement_id"],
        "started_at": iso(row["started_at"]),
        "resolved_at": iso(row["resolved_at"]),
        "acknowledged_at": iso(row["acknowledged_at"]),
        "acknowledged_by": row["acknowledged_by"],
        "acknowledged_by_name": row.get("acknowledged_by_name"),
    }
