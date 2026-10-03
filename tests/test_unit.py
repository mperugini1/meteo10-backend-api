"""Testes unitários (sem banco)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import jwt
import pytest

from app.db.migrate import split_statements
from app.fmt import number_br, with_unit
from app.security import gateway_signature
from app.security.passwords import hash_password, verify_password
from app.security.rate_limit import SlidingWindowLimiter
from app.security.tokens import create_access_token, decode_access_token
from app.services.alarms import rule_cleared, rule_description, rule_triggered
from app.services.processing import (
    FLAG_FAULT_BMP280,
    FLAG_FAULT_DHT22,
    FLAG_FAULT_SOIL,
    FLAG_FAULT_UV,
    FLAG_FAULT_WIND,
    battery_pct,
    convert,
    estimate_measured_at,
    int16,
    seq_distance,
)
from app.services.readings import availability, expected_count, is_online, vector_mean_direction

RAW = {
    "temperature_raw": 2357,
    "humidity_raw": 6420,
    "pressure_raw": 9235,
    "wind_speed_raw": 312,
    "wind_direction_raw": 135,
    "soil_moisture_raw": 41,
    "uv_index_raw": 63,
    "battery_mv": 3987,
}


# --- Conversão --------------------------------------------------------------

def test_convert_example_packet():
    result = convert(RAW, 0)
    v = result.values
    assert v["temperature_c"] == Decimal("23.57")
    assert v["humidity_pct"] == Decimal("64.2")
    assert v["pressure_hpa"] == Decimal("923.5")
    assert v["wind_speed_ms"] == Decimal("3.12")
    assert v["wind_direction_deg"] == 135
    assert v["soil_moisture_pct"] == 41
    assert v["uv_index"] == Decimal("6.3")
    assert v["battery_v"] == Decimal("3.987")
    assert result.quality_flags is None


@pytest.mark.parametrize("raw, expected", [(-550, Decimal("-5.5")), (65536 - 550, Decimal("-5.5")), (0, Decimal(0))])
def test_negative_temperature_int16(raw, expected):
    assert convert({**RAW, "temperature_raw": raw}, 0).values["temperature_c"] == expected


def test_int16_helper():
    assert int16(0x7FFF) == 32767
    assert int16(0x8000) == -32768
    assert int16(0xFFFF) == -1


@pytest.mark.parametrize(
    "field, raw, column, short",
    [
        ("temperature_raw", 8001, "temperature_c", "temperature"),
        ("temperature_raw", -4001, "temperature_c", "temperature"),
        ("humidity_raw", 10001, "humidity_pct", "humidity"),
        ("pressure_raw", 2999, "pressure_hpa", "pressure"),
        ("pressure_raw", 11001, "pressure_hpa", "pressure"),
        ("wind_speed_raw", 6001, "wind_speed_ms", "wind_speed"),
        ("wind_direction_raw", 360, "wind_direction_deg", "wind_direction"),
        ("soil_moisture_raw", 101, "soil_moisture_pct", "soil_moisture"),
        ("uv_index_raw", 201, "uv_index", "uv_index"),
        ("battery_mv", 2499, "battery_v", "battery"),
        ("battery_mv", 4301, "battery_v", "battery"),
    ],
)
def test_out_of_range_becomes_null(field, raw, column, short):
    result = convert({**RAW, field: raw}, 0)
    assert result.values[column] is None
    assert result.quality_flags == {"out_of_range": [short]}


def test_range_limits_are_inclusive():
    result = convert({**RAW, "temperature_raw": 8000, "pressure_raw": 3000, "battery_mv": 4300, "wind_direction_raw": 359}, 0)
    assert result.values["temperature_c"] == Decimal(80)
    assert result.values["pressure_hpa"] == Decimal(300)
    assert result.values["battery_v"] == Decimal("4.3")
    assert result.values["wind_direction_deg"] == 359


@pytest.mark.parametrize(
    "flag, columns, sensor",
    [
        (FLAG_FAULT_DHT22, ("temperature_c", "humidity_pct"), "dht22"),
        (FLAG_FAULT_BMP280, ("pressure_hpa",), "bmp280"),
        (FLAG_FAULT_WIND, ("wind_speed_ms", "wind_direction_deg"), "wind"),
        (FLAG_FAULT_SOIL, ("soil_moisture_pct",), "soil"),
        (FLAG_FAULT_UV, ("uv_index",), "uv"),
    ],
)
def test_fault_bits_null_columns(flag, columns, sensor):
    result = convert(RAW, flag)
    for column in columns:
        assert result.values[column] is None
    others = [c for c in result.values if c not in columns]
    assert all(result.values[c] is not None for c in others)
    assert result.quality_flags == {"sensor_fault": [sensor]}


def test_faulty_sensor_value_not_reported_out_of_range():
    result = convert({**RAW, "pressure_raw": 0}, FLAG_FAULT_BMP280)
    assert result.out_of_range == []


# --- Sequência e horário estimado --------------------------------------------

def test_seq_distance_wraparound():
    assert seq_distance(5, 65530) == 11
    assert seq_distance(100, 90) == 10


def test_estimate_measured_at():
    received = datetime(2026, 10, 3, 14, 20)
    assert estimate_measured_at(received, 1530, 1534, 600) == received - timedelta(seconds=2400)
    # volta do contador: atual 3, retransmitida 65534 -> 5 passos
    assert estimate_measured_at(received, 65534, 3, 600) == received - timedelta(seconds=3000)


def test_estimate_without_reliable_reference():
    received = datetime(2026, 10, 3, 14, 20)
    assert estimate_measured_at(received, 10, None, 600) == received
    assert estimate_measured_at(received, 10, 10, 600) == received
    assert estimate_measured_at(received, 20, 10, 600) == received  # "à frente" do atual


# --- Status derivados ---------------------------------------------------------

@pytest.mark.parametrize("volts, pct", [(Decimal("3.0"), 0), (Decimal("4.2"), 100), (Decimal("3.6"), 50), (Decimal("2.8"), 0), (Decimal("4.3"), 100), (None, None)])
def test_battery_pct(volts, pct):
    assert battery_pct(volts) == pct


def test_is_online():
    now = datetime(2026, 10, 3, 12, 0)
    assert is_online(now - timedelta(minutes=30), 600, now, 3)
    assert not is_online(now - timedelta(minutes=30, seconds=1), 600, now, 3)
    assert not is_online(None, 600, now, 3)


def test_availability():
    start = datetime(2026, 10, 1)
    assert expected_count(start, start + timedelta(days=1), 600) == 144
    assert availability(144, 72) == 50.0
    assert availability(144, 200) == 100.0
    assert availability(0, 0) is None


def test_vector_mean_direction_wraps_north():
    import math

    dirs = [350, 10]
    s = sum(math.sin(math.radians(d)) for d in dirs)
    c = sum(math.cos(math.radians(d)) for d in dirs)
    assert vector_mean_direction(s, c, 2) == 0
    dirs = [90, 180]
    s = sum(math.sin(math.radians(d)) for d in dirs)
    c = sum(math.cos(math.radians(d)) for d in dirs)
    assert vector_mean_direction(s, c, 2) == 135
    assert vector_mean_direction(0.0, 0.0, 2) is None  # direções opostas
    assert vector_mean_direction(None, None, 0) is None


# --- Regras de alarme ---------------------------------------------------------

def test_rule_trigger_and_hysteresis():
    assert rule_triggered("gt", Decimal("35.1"), Decimal(35))
    assert not rule_triggered("gt", Decimal(35), Decimal(35))
    assert rule_triggered("lt", Decimal("1.9"), Decimal(2))
    assert not rule_cleared("gt", Decimal("34.5"), Decimal(35), Decimal(1))
    assert rule_cleared("gt", Decimal(34), Decimal(35), Decimal(1))
    assert not rule_cleared("lt", Decimal("2.5"), Decimal(2), Decimal(1))
    assert rule_cleared("lt", Decimal(3), Decimal(2), Decimal(1))


def test_rule_description_pt_br():
    assert rule_description("temperature_c", "gt", Decimal("35.00")) == "Temperatura acima de 35,0 °C"
    assert rule_description("humidity_pct", "lt", Decimal("30")) == "Umidade do ar abaixo de 30,0%"


def test_number_br():
    assert number_br(Decimal("1234.5"), 1) == "1.234,5"
    assert number_br(-5.25, 2) == "-5,25"
    assert with_unit("battery_v", Decimal("3.25")) == "3,25 V"


# --- Segurança ---------------------------------------------------------------

def test_password_hash_and_verify():
    h, salt = hash_password("segredo", iterations=1000)
    assert len(h) == 64 and len(salt) == 32
    assert verify_password("segredo", h, salt, iterations=1000)
    assert not verify_password("outro", h, salt, iterations=1000)
    h2, salt2 = hash_password("segredo", iterations=1000)
    assert salt2 != salt and h2 != h


def test_jwt_roundtrip_and_expiry():
    token, expires_in = create_access_token(7, "viewer")
    claims = decode_access_token(token)
    assert claims["sub"] == "7" and claims["role"] == "viewer"
    assert expires_in == 480 * 60
    old, _ = create_access_token(7, "viewer", now=datetime.now(timezone.utc) - timedelta(days=1))
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(old)


def test_cmac_rfc4493_vector():
    """Vetor de teste da RFC 4493 (exemplo 2) garante compatibilidade com o firmware."""
    from cryptography.hazmat.primitives import cmac
    from cryptography.hazmat.primitives.ciphers import algorithms

    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    mac = cmac.CMAC(algorithms.AES(key))
    mac.update(bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"))
    assert mac.finalize().hex() == "070a16b46b4d4144f79bdd9dd04a287c"


def test_gateway_signature_message_and_verify():
    key = bytes(range(16))
    body = b'{"station_id":1}'
    msg = gateway_signature.signing_message(1, 1791043200, "ab" * 16, body)
    assert msg.startswith(b"1\n1791043200\n" + b"ab" * 16 + b"\n")
    sig = gateway_signature.sign(key, 1, 1791043200, "ab" * 16, body)
    assert len(sig) == 32 and sig == sig.lower()
    assert gateway_signature.verify(key, 1, 1791043200, "ab" * 16, body, sig)
    assert not gateway_signature.verify(key, 1, 1791043200, "ab" * 16, body + b" ", sig)
    assert not gateway_signature.verify(key, 1, 1791043201, "ab" * 16, body, sig)
    assert not gateway_signature.verify(key, 1, 1791043200, "ab" * 16, body, "zz")


def test_rate_limiter():
    limiter = SlidingWindowLimiter(max_attempts=3, window_s=60)
    for _ in range(3):
        assert not limiter.is_blocked("1.2.3.4")
        limiter.register_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4")
    assert not limiter.is_blocked("5.6.7.8")


def test_split_statements():
    sql = "-- comentário\nCREATE TABLE a (x INT);\n\nALTER TABLE a\n  ADD COLUMN y INT;\n"
    assert split_statements(sql) == ["CREATE TABLE a (x INT)", "ALTER TABLE a\n  ADD COLUMN y INT"]
