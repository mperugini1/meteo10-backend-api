"""Schemas Pydantic v2 das entradas da API."""

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

ALARM_VARIABLES = (
    "temperature_c",
    "humidity_pct",
    "pressure_hpa",
    "wind_speed_ms",
    "soil_moisture_pct",
    "uv_index",
    "battery_v",
)
AlarmVariable = Literal[
    "temperature_c", "humidity_pct", "pressure_hpa", "wind_speed_ms", "soil_moisture_pct", "uv_index", "battery_v"
]


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 190 or not _EMAIL_RE.match(value):
        raise ValueError("e-mail inválido")
    return value


# Validação simples: domínios como meteo10.local são aceitos (o email-validator os rejeitaria)
Email = Annotated[str, AfterValidator(_email)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --- Ingestão --------------------------------------------------------------

class IngestPayload(BaseModel):
    """Corpo enviado pelo gateway (seção 6.3). Campos brutos inteiros."""

    model_config = ConfigDict(extra="ignore")

    station_id: int = Field(ge=1, le=255)
    seq: int = Field(ge=0, le=65535)
    flags: int = Field(ge=0, le=255)
    # int16: aceita com sinal (-32768..32767) ou a representação sem sinal (0..65535)
    temperature_raw: int = Field(ge=-32768, le=65535)
    humidity_raw: int = Field(ge=0, le=65535)
    pressure_raw: int = Field(ge=0, le=65535)
    wind_speed_raw: int = Field(ge=0, le=65535)
    wind_direction_raw: int = Field(ge=0, le=65535)
    soil_moisture_raw: int = Field(ge=0, le=255)
    uv_index_raw: int = Field(ge=0, le=255)
    battery_mv: int = Field(ge=0, le=65535)
    rssi_dbm: int | None = Field(default=None, ge=-200, le=50)
    snr_db: float | None = Field(default=None, ge=-99.9, le=99.9)
    received_at: datetime | None = None

    @field_validator("received_at")
    @classmethod
    def _tz_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("received_at deve informar o fuso (use 'Z' para UTC)")
        return value


# --- Autenticação ----------------------------------------------------------

class LoginRequest(StrictModel):
    email: Email
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordRequest(StrictModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


# --- Estações --------------------------------------------------------------

class StationBase(StrictModel):
    description: str | None = Field(default=None, max_length=500)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90, max_digits=9, decimal_places=6)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180, max_digits=9, decimal_places=6)
    altitude_m: int | None = Field(default=None, ge=-500, le=9000)


class StationCreate(StationBase):
    id: int = Field(ge=1, le=255)
    name: str = Field(min_length=1, max_length=120)
    measurement_interval_s: int = Field(default=600, ge=10, le=86400)
    is_active: bool = True


class StationUpdate(StationBase):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    measurement_interval_s: int | None = Field(default=None, ge=10, le=86400)
    is_active: bool | None = None


# --- Alarmes ---------------------------------------------------------------

class AlarmRuleCreate(StrictModel):
    variable: AlarmVariable
    operator: Literal["gt", "lt"]
    threshold: Decimal = Field(ge=-999999, le=999999, decimal_places=2)
    hysteresis: Decimal = Field(default=Decimal(0), ge=0, le=999999, decimal_places=2)
    is_enabled: bool = True


class AlarmRuleUpdate(StrictModel):
    variable: AlarmVariable | None = None
    operator: Literal["gt", "lt"] | None = None
    threshold: Decimal | None = Field(default=None, ge=-999999, le=999999, decimal_places=2)
    hysteresis: Decimal | None = Field(default=None, ge=0, le=999999, decimal_places=2)
    is_enabled: bool | None = None


# --- Administração ---------------------------------------------------------

class GatewayCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    is_active: bool = True


class GatewayUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None


class UserCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    email: Email
    password: str = Field(min_length=8, max_length=200)
    role: Literal["admin", "viewer"] = "viewer"
    is_active: bool = True


class UserUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    email: Email | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200)
    role: Literal["admin", "viewer"] | None = None
    is_active: bool | None = None
