"""Conversão dos valores brutos do pacote para unidades físicas (seções 4.1–4.4).

Funções puras, sem acesso ao banco: o backend é o único ponto de verdade da conversão.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

# Bits do campo flags (seção 4.3)
FLAG_NODE_ALARM = 1 << 0
FLAG_RETRANSMITTED = 1 << 1
FLAG_FAULT_DHT22 = 1 << 2
FLAG_FAULT_BMP280 = 1 << 3
FLAG_FAULT_WIND = 1 << 4
FLAG_FAULT_SOIL = 1 << 5
FLAG_FAULT_UV = 1 << 6

SEQ_MODULO = 65536

# Sensor -> (bit de falha, colunas afetadas)
SENSORS: dict[str, tuple[int, tuple[str, ...]]] = {
    "dht22": (FLAG_FAULT_DHT22, ("temperature_c", "humidity_pct")),
    "bmp280": (FLAG_FAULT_BMP280, ("pressure_hpa",)),
    "wind": (FLAG_FAULT_WIND, ("wind_speed_ms", "wind_direction_deg")),
    "soil": (FLAG_FAULT_SOIL, ("soil_moisture_pct",)),
    "uv": (FLAG_FAULT_UV, ("uv_index",)),
}

SENSOR_LABELS = {
    "dht22": "sensor de temperatura e umidade (DHT22)",
    "bmp280": "sensor de pressão (BMP280)",
    "wind": "sensores de vento",
    "soil": "sensor de umidade do solo",
    "uv": "sensor UV",
}


@dataclass(frozen=True)
class Variable:
    column: str
    raw_field: str
    short_name: str  # usado em quality_flags
    divisor: Decimal
    minimum: Decimal
    maximum: Decimal
    integer: bool = False


VARIABLES: tuple[Variable, ...] = (
    Variable("temperature_c", "temperature_raw", "temperature", Decimal(100), Decimal("-40.0"), Decimal("80.0")),
    Variable("humidity_pct", "humidity_raw", "humidity", Decimal(100), Decimal("0.0"), Decimal("100.0")),
    Variable("pressure_hpa", "pressure_raw", "pressure", Decimal(10), Decimal("300.0"), Decimal("1100.0")),
    Variable("wind_speed_ms", "wind_speed_raw", "wind_speed", Decimal(100), Decimal("0.0"), Decimal("60.0")),
    Variable("wind_direction_deg", "wind_direction_raw", "wind_direction", Decimal(1), Decimal(0), Decimal(359), integer=True),
    Variable("soil_moisture_pct", "soil_moisture_raw", "soil_moisture", Decimal(1), Decimal(0), Decimal(100), integer=True),
    Variable("uv_index", "uv_index_raw", "uv_index", Decimal(10), Decimal("0.0"), Decimal("20.0")),
    Variable("battery_v", "battery_mv", "battery", Decimal(1000), Decimal("2.50"), Decimal("4.30")),
)

VALUE_COLUMNS = tuple(v.column for v in VARIABLES)


def int16(raw: int) -> int:
    """Interpreta como int16 com sinal (aceita a representação sem sinal 0–65535)."""
    return raw - SEQ_MODULO if raw > 0x7FFF else raw


@dataclass
class ProcessedValues:
    values: dict[str, Decimal | int | None]
    faulty_sensors: list[str] = field(default_factory=list)
    out_of_range: list[str] = field(default_factory=list)

    @property
    def quality_flags(self) -> dict[str, Any] | None:
        flags: dict[str, Any] = {}
        if self.out_of_range:
            flags["out_of_range"] = self.out_of_range
        if self.faulty_sensors:
            flags["sensor_fault"] = self.faulty_sensors
        return flags or None


def faulty_sensors_from_flags(flags: int) -> list[str]:
    return [name for name, (bit, _) in SENSORS.items() if flags & bit]


def convert(raw: dict[str, int], flags: int) -> ProcessedValues:
    """Converte os campos brutos, aplicando bits de falha e validação de faixa."""
    faulty = faulty_sensors_from_flags(flags)
    faulty_columns = {col for name in faulty for col in SENSORS[name][1]}
    values: dict[str, Decimal | int | None] = {}
    out_of_range: list[str] = []
    for var in VARIABLES:
        raw_value = raw[var.raw_field]
        if var.column in faulty_columns:
            values[var.column] = None
            continue
        if var.column == "temperature_c":
            raw_value = int16(raw_value)
        value = Decimal(raw_value) / var.divisor
        if value < var.minimum or value > var.maximum:
            values[var.column] = None
            out_of_range.append(var.short_name)
            continue
        values[var.column] = int(value) if var.integer else value
    return ProcessedValues(values=values, faulty_sensors=faulty, out_of_range=out_of_range)


def seq_distance(newer: int, older: int) -> int:
    """Quantos passos de 'older' até 'newer', considerando a volta do contador uint16."""
    return (newer - older) % SEQ_MODULO


def estimate_measured_at(received_at: datetime, seq: int, reference_seq: int | None, interval_s: int) -> datetime:
    """Estima o horário de uma medição retransmitida (seção 4.4, passo 5).

    measured_at = received_at − (seq_atual − seq) × intervalo. Sem referência confiável
    (sem seq de referência, distância nula ou maior que meio ciclo do contador) usa received_at.
    """
    if reference_seq is None:
        return received_at
    distance = seq_distance(reference_seq, seq)
    if distance == 0 or distance >= SEQ_MODULO // 2:
        return received_at
    return received_at - timedelta(seconds=distance * interval_s)


def battery_pct(battery_v: Decimal | float | None) -> int | None:
    """Estimativa linear: 3,00 V = 0 %, 4,20 V = 100 %, limitada a 0–100."""
    if battery_v is None:
        return None
    pct = (float(battery_v) - 3.0) / 1.2 * 100
    return int(round(max(0.0, min(100.0, pct))))
