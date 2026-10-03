"""Formatação pt-BR usada nas mensagens geradas pelo backend (alarmes, CSV)."""

from decimal import ROUND_HALF_UP, Decimal


def number_br(value: Decimal | float | int | None, decimals: int = 1) -> str:
    """Número com vírgula decimal e ponto de milhar, arredondando meio para cima."""
    if value is None:
        return ""
    quantized = Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    text = f"{quantized:,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


VARIABLE_LABELS = {
    "temperature_c": ("temperatura", "°C", 1),
    "humidity_pct": ("umidade do ar", "%", 1),
    "pressure_hpa": ("pressão", "hPa", 1),
    "wind_speed_ms": ("velocidade do vento", "m/s", 1),
    "wind_direction_deg": ("direção do vento", "°", 0),
    "soil_moisture_pct": ("umidade do solo", "%", 0),
    "uv_index": ("índice UV", "", 1),
    "battery_v": ("bateria", "V", 2),
}


def with_unit(variable: str, value: Decimal | float | int) -> str:
    _, unit, decimals = VARIABLE_LABELS[variable]
    text = number_br(value, decimals)
    if not unit:
        return text
    return f"{text}{unit}" if unit == "%" else f"{text} {unit}"
