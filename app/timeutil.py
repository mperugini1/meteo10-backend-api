"""Datas: armazenadas como DATETIME UTC sem fuso; expostas em ISO 8601 com 'Z'."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def utcnow() -> datetime:
    """Agora em UTC, sem fuso (formato das colunas DATETIME), com precisão de milissegundos."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TZ)


def local_offset_seconds(at: datetime) -> int:
    """Deslocamento de America/Sao_Paulo em relação ao UTC no instante dado (ex.: -10800)."""
    offset = to_local(at).utcoffset()
    return int(offset.total_seconds()) if offset else 0
