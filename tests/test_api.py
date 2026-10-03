"""Testes de API com banco MySQL (ingestão, alarmes, consultas, autorização)."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.db import pool
from app.services import alarms
from app.services.processing import FLAG_FAULT_DHT22, FLAG_RETRANSMITTED
from app.timeutil import utcnow
from tests.conftest import ADMIN, GATEWAY_SECRET, VIEWER, iso, make_payload, signed_headers

API = "/api/v1"


def q(sql, params=()):
    with pool.connection() as conn:
        return pool.query_all(conn, sql, params, json_columns=("quality_flags", "raw_payload"))


def open_events(type_=None):
    sql = "SELECT * FROM alarm_events WHERE resolved_at IS NULL"
    return [e for e in q(sql) if type_ is None or e["type"] == type_]


# ---------------------------------------------------------------------------
# Saúde e autenticação
# ---------------------------------------------------------------------------

def test_health(client):
    assert client.get(f"{API}/health").json() == {"status": "ok", "db": "ok"}


def test_login_me_and_errors(client):
    r = client.post(f"{API}/auth/login", json=ADMIN)
    body = r.json()
    assert r.status_code == 200
    assert body["token_type"] == "bearer" and body["expires_in"] == 480 * 60
    assert body["user"]["role"] == "admin" and "password_hash" not in body["user"]
    me = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.json()["email"] == ADMIN["email"]

    r = client.post(f"{API}/auth/login", json={**ADMIN, "password": "errada"})
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "invalid_credentials", "message": "E-mail ou senha incorretos."}}
    assert client.get(f"{API}/auth/me").status_code == 401
    assert client.get(f"{API}/auth/me", headers={"Authorization": "Bearer xyz"}).json()["error"]["code"] == "invalid_token"


def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post(f"{API}/auth/login", json={**ADMIN, "password": "errada"}).status_code == 401
    r = client.post(f"{API}/auth/login", json=ADMIN)
    assert r.status_code == 429


def test_change_password(client, viewer_headers):
    r = client.post(f"{API}/auth/change-password", headers=viewer_headers,
                    json={"current_password": "errada", "new_password": "nova-senha-123"})
    assert r.status_code == 400
    r = client.post(f"{API}/auth/change-password", headers=viewer_headers,
                    json={"current_password": VIEWER["password"], "new_password": "nova-senha-123"})
    assert r.status_code == 200
    assert client.post(f"{API}/auth/login", json={**VIEWER, "password": "nova-senha-123"}).status_code == 200


def test_viewer_cannot_access_admin_routes(client, viewer_headers):
    for method, path, body in [
        ("get", "/gateways", None),
        ("post", "/gateways", {"name": "x"}),
        ("post", "/gateways/1/rotate-secret", None),
        ("patch", "/gateways/1", {"name": "x"}),
        ("get", "/users", None),
        ("post", "/users", {"name": "x", "email": "x@x.com", "password": "12345678"}),
        ("patch", "/users/1", {"name": "x"}),
        ("post", "/stations", {"id": 9, "name": "x"}),
        ("patch", "/stations/1", {"name": "x"}),
        ("delete", "/stations/1", None),
    ]:
        r = getattr(client, method)(f"{API}{path}", headers=viewer_headers, **({"json": body} if body else {}))
        assert r.status_code == 403, (method, path)
        assert r.json()["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Ingestão
# ---------------------------------------------------------------------------

def test_ingest_created_and_converted(send):
    r = send(seq=10)
    assert r.status_code == 201
    mid = r.json()["measurement_id"]
    row = q("SELECT * FROM measurements WHERE id = %s", (mid,))[0]
    assert float(row["temperature_c"]) == 23.57
    assert float(row["pressure_hpa"]) == 923.5
    assert float(row["battery_v"]) == 3.987
    assert row["gateway_id"] == 1 and row["quality_flags"] is None
    assert row["raw_payload"]["temperature_raw"] == 2357
    assert row["measured_at"] == row["received_at"] and not row["measured_at_estimated"]
    station = q("SELECT * FROM stations WHERE id = 1")[0]
    assert station["last_seq"] == 10 and station["last_battery_mv"] == 3987 and station["last_seen_at"] is not None
    assert q("SELECT last_seen_at FROM gateways WHERE id = 1")[0]["last_seen_at"] is not None


def test_ingest_fault_and_out_of_range(send):
    mid = send(flags=FLAG_FAULT_DHT22, pressure_raw=200).json()["measurement_id"]
    row = q("SELECT * FROM measurements WHERE id = %s", (mid,))[0]
    assert row["temperature_c"] is None and row["humidity_pct"] is None and row["pressure_hpa"] is None
    assert row["wind_speed_ms"] is not None
    assert row["quality_flags"] == {"out_of_range": ["pressure"], "sensor_fault": ["dht22"]}


def test_ingest_negative_temperature(send):
    mid = send(temperature_raw=-325).json()["measurement_id"]
    assert float(q("SELECT temperature_c FROM measurements WHERE id = %s", (mid,))[0]["temperature_c"]) == -3.25


def test_duplicate_is_idempotent(send):
    assert send(seq=42).status_code == 201
    r = send(seq=42)  # mesmo pacote, novo nonce (reenvio do gateway)
    assert r.status_code == 200 and r.json() == {"status": "duplicate"}
    assert len(q("SELECT id FROM measurements")) == 1


def test_duplicate_window_after_counter_wrap(send):
    old = datetime.now(timezone.utc) - timedelta(days=2)
    assert send(seq=5, received_at=old).status_code == 201
    # Depois da volta do contador o mesmo seq reaparece mais de 24 h depois: não é duplicata
    assert send(seq=65535, received_at=old + timedelta(days=1, hours=12)).status_code == 201
    assert send(seq=5, received_at=datetime.now(timezone.utc)).status_code == 201
    assert len(q("SELECT id FROM measurements WHERE seq = 5")) == 2


def test_unknown_or_inactive_station(client, send, admin_headers):
    r = send(station_id=99)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "station_not_found"
    client.delete(f"{API}/stations/1", headers=admin_headers)
    assert send().status_code == 404


def test_invalid_body_is_422(client):
    body = json.dumps(make_payload(seq=70000)).encode()
    r = client.post(f"{API}/ingest/measurements", content=body, headers=signed_headers(body))
    assert r.status_code == 422
    body = b"nao-json"
    assert client.post(f"{API}/ingest/measurements", content=body, headers=signed_headers(body)).status_code == 422


def test_signature_validation(client):
    body = json.dumps(make_payload()).encode()
    url = f"{API}/ingest/measurements"

    # sem cabeçalhos
    assert client.post(url, content=body).status_code == 401
    # assinatura com outra chave
    r = client.post(url, content=body, headers=signed_headers(body, secret=bytes(16)))
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_signature"
    # corpo alterado após assinar
    headers = signed_headers(body)
    assert client.post(url, content=body.replace(b"1534", b"1535"), headers=headers).status_code == 401
    # timestamp fora da janela de 300 s
    r = client.post(url, content=body, headers=signed_headers(body, timestamp=int(time.time()) - 301))
    assert r.status_code == 401 and r.json()["error"]["code"] == "timestamp_out_of_window"
    # gateway desconhecido
    assert client.post(url, content=body, headers=signed_headers(body, gateway_id=9)).status_code == 401
    # nonce repetido
    headers = signed_headers(body, nonce="ab" * 16)
    assert client.post(url, content=body, headers=headers).status_code == 201
    r = client.post(url, content=body, headers=headers)
    assert r.status_code == 401 and r.json()["error"]["code"] == "nonce_reused"
    assert len(q("SELECT id FROM measurements")) == 1


def test_inactive_gateway_rejected(client, admin_headers, send):
    client.patch(f"{API}/gateways/1", headers=admin_headers, json={"is_active": False})
    assert send().status_code == 401


def test_retransmission_estimated_time(send):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    send(seq=1534, received_at=now)
    later = now + timedelta(seconds=5)
    mid = send(seq=1530, flags=FLAG_RETRANSMITTED, received_at=later).json()["measurement_id"]
    row = q("SELECT * FROM measurements WHERE id = %s", (mid,))[0]
    assert row["measured_at_estimated"]
    assert row["measured_at"] == later.replace(tzinfo=None) - timedelta(seconds=4 * 600)
    station = q("SELECT * FROM stations WHERE id = 1")[0]
    assert station["last_seq"] == 1534  # retransmissão não altera o estado atual


def test_retransmission_after_counter_wrap(send):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    send(seq=2, received_at=now)
    mid = send(seq=65534, flags=FLAG_RETRANSMITTED, received_at=now).json()["measurement_id"]
    row = q("SELECT measured_at FROM measurements WHERE id = %s", (mid,))[0]
    assert row["measured_at"] == now.replace(tzinfo=None) - timedelta(seconds=4 * 600)


def test_retransmission_without_reference(send):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    mid = send(seq=7, flags=FLAG_RETRANSMITTED, received_at=now).json()["measurement_id"]
    row = q("SELECT * FROM measurements WHERE id = %s", (mid,))[0]
    assert row["measured_at_estimated"] and row["measured_at"] == now.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Alarmes
# ---------------------------------------------------------------------------

def test_rule_open_hysteresis_resolve_and_ack(client, send, viewer_headers, base_time):
    r = client.post(f"{API}/stations/1/alarm-rules", headers=viewer_headers,
                    json={"variable": "temperature_c", "operator": "gt", "threshold": 25, "hysteresis": 1})
    assert r.status_code == 201
    rule_id = r.json()["id"]

    send(seq=1, temperature_raw=2400, received_at=base_time)
    assert open_events() == []
    send(seq=2, temperature_raw=2600, received_at=base_time + timedelta(minutes=10))
    events = open_events("rule")
    assert len(events) == 1 and events[0]["rule_id"] == rule_id
    assert events[0]["message"] == "Temperatura acima de 25,0 °C (valor medido: 26,0 °C)"
    send(seq=3, temperature_raw=2700, received_at=base_time + timedelta(minutes=20))
    assert len(q("SELECT id FROM alarm_events")) == 1  # não duplica evento aberto
    send(seq=4, temperature_raw=2450, received_at=base_time + timedelta(minutes=30))
    assert len(open_events("rule")) == 1  # dentro da histerese
    event_id = events[0]["id"]

    r = client.post(f"{API}/alarm-events/{event_id}/acknowledge", headers=viewer_headers)
    assert r.status_code == 200
    assert r.json()["acknowledged_at"] is not None and r.json()["status"] == "active"
    assert r.json()["acknowledged_by_name"] == "Viewer"

    send(seq=5, temperature_raw=2390, received_at=base_time + timedelta(minutes=40))
    assert open_events("rule") == []
    listed = client.get(f"{API}/alarm-events?status=resolved", headers=viewer_headers).json()["items"]
    assert [e["id"] for e in listed] == [event_id] and listed[0]["resolved_at"] is not None
    assert client.get(f"{API}/alarm-events?status=active", headers=viewer_headers).json()["items"] == []


def test_rule_lt_and_crud(client, send, viewer_headers):
    rule = client.post(f"{API}/stations/1/alarm-rules", headers=viewer_headers,
                       json={"variable": "humidity_pct", "operator": "lt", "threshold": 30}).json()
    send(seq=1, humidity_raw=2500)
    assert len(open_events("rule")) == 1
    r = client.patch(f"{API}/alarm-rules/{rule['id']}", headers=viewer_headers, json={"is_enabled": False})
    assert r.status_code == 200 and r.json()["is_enabled"] is False
    assert open_events("rule") == []  # desativar a regra encerra o evento
    assert len(client.get(f"{API}/stations/1/alarm-rules", headers=viewer_headers).json()["items"]) == 1
    assert client.delete(f"{API}/alarm-rules/{rule['id']}", headers=viewer_headers).status_code == 204
    assert client.get(f"{API}/stations/1/alarm-rules", headers=viewer_headers).json()["items"] == []
    assert q("SELECT rule_id FROM alarm_events")[0]["rule_id"] is None  # histórico preservado


def test_rule_validation(client, viewer_headers):
    r = client.post(f"{API}/stations/1/alarm-rules", headers=viewer_headers,
                    json={"variable": "wind_direction_deg", "operator": "gt", "threshold": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_battery_alarms(send, base_time):
    send(seq=1, battery_mv=3250, received_at=base_time)
    assert [e["severity"] for e in open_events("battery_low")] == ["warning"]
    assert open_events("battery_critical") == []
    send(seq=2, battery_mv=2950, received_at=base_time + timedelta(minutes=10))
    critical = open_events("battery_critical")
    assert len(critical) == 1 and critical[0]["severity"] == "critical"
    assert "2,95 V" in critical[0]["message"]
    send(seq=3, battery_mv=3150, received_at=base_time + timedelta(minutes=20))
    assert open_events("battery_critical") == []  # >= 3100 mV
    assert len(open_events("battery_low")) == 1
    send(seq=4, battery_mv=3350, received_at=base_time + timedelta(minutes=30))
    assert len(open_events("battery_low")) == 1  # histerese de 100 mV
    send(seq=5, battery_mv=3400, received_at=base_time + timedelta(minutes=40))
    assert open_events() == []


def test_sensor_fault_after_three_packets(send, base_time):
    for i in range(2):
        send(seq=i, flags=FLAG_FAULT_DHT22, received_at=base_time + timedelta(minutes=10 * i))
    assert open_events() == []
    send(seq=2, flags=FLAG_FAULT_DHT22, received_at=base_time + timedelta(minutes=20))
    events = open_events("sensor_fault")
    assert len(events) == 1 and events[0]["source"] == "dht22"
    send(seq=3, flags=FLAG_FAULT_DHT22, received_at=base_time + timedelta(minutes=30))
    assert len(q("SELECT id FROM alarm_events")) == 1
    send(seq=4, received_at=base_time + timedelta(minutes=40))
    assert open_events() == []


def test_sensor_fault_interrupted_sequence(send, base_time):
    send(seq=0, flags=FLAG_FAULT_DHT22, received_at=base_time)
    send(seq=1, received_at=base_time + timedelta(minutes=10))
    send(seq=2, flags=FLAG_FAULT_DHT22, received_at=base_time + timedelta(minutes=20))
    send(seq=3, flags=FLAG_FAULT_DHT22, received_at=base_time + timedelta(minutes=30))
    assert open_events() == []


def test_station_offline(client, send, viewer_headers):
    send(seq=1, received_at=datetime.now(timezone.utc) - timedelta(minutes=31))
    station = client.get(f"{API}/stations/1", headers=viewer_headers).json()
    assert station["online"] is False
    with pool.connection() as conn:
        opened = alarms.check_offline_stations(conn, utcnow(), 3)
        conn.commit()
        assert len(opened) == 1
        assert alarms.check_offline_stations(conn, utcnow(), 3) == []  # não duplica
        conn.commit()
    assert open_events("station_offline")[0]["message"] == "Estação sem comunicação há mais de 30 min."
    send(seq=2)
    assert open_events("station_offline") == []
    assert client.get(f"{API}/stations/1", headers=viewer_headers).json()["online"] is True


# ---------------------------------------------------------------------------
# Estações e medições
# ---------------------------------------------------------------------------

def test_station_crud(client, admin_headers, viewer_headers):
    r = client.post(f"{API}/stations", headers=admin_headers, json={"id": 2, "name": "Horta", "latitude": -23.5})
    assert r.status_code == 201 and r.json()["online"] is False and r.json()["measurement_interval_s"] == 600
    assert client.post(f"{API}/stations", headers=admin_headers, json={"id": 2, "name": "X"}).status_code == 409
    assert client.post(f"{API}/stations", headers=admin_headers, json={"id": 256, "name": "X"}).status_code == 422
    r = client.patch(f"{API}/stations/2", headers=admin_headers, json={"measurement_interval_s": 300, "description": "Canteiro"})
    assert r.json()["measurement_interval_s"] == 300 and r.json()["description"] == "Canteiro"
    assert client.delete(f"{API}/stations/2", headers=admin_headers).json()["is_active"] is False
    ids = [s["id"] for s in client.get(f"{API}/stations", headers=viewer_headers).json()["items"]]
    assert ids == [1]
    ids = [s["id"] for s in client.get(f"{API}/stations?include_inactive=true", headers=admin_headers).json()["items"]]
    assert ids == [1, 2]
    assert client.get(f"{API}/stations/99", headers=viewer_headers).status_code == 404


def test_station_list_status(client, send, viewer_headers):
    send(seq=1, battery_mv=3600)
    send(seq=2, flags=FLAG_FAULT_DHT22, battery_mv=3600)
    station = client.get(f"{API}/stations", headers=viewer_headers).json()["items"][0]
    assert station["online"] is True
    assert station["battery_v"] == 3.6 and station["battery_pct"] == 50
    assert station["latest"]["temperature_c"] == 23.57  # última leitura válida, apesar da falha
    assert station["active_alarms_count"] == 0
    latest = client.get(f"{API}/stations/1/latest", headers=viewer_headers).json()
    assert latest["values"]["temperature_c"]["value"] == 23.57
    assert latest["values"]["pressure_hpa"]["measured_at"].endswith("Z")
    assert latest["values"]["temperature_c"]["measured_at"] < latest["values"]["pressure_hpa"]["measured_at"]


def test_measurements_pagination(client, send, viewer_headers, base_time):
    for i in range(5):
        send(seq=i, received_at=base_time + timedelta(minutes=10 * i))
    url = f"{API}/stations/1/measurements?limit=2&from={iso(base_time - timedelta(hours=1))}"
    page1 = client.get(url, headers=viewer_headers).json()
    assert [m["seq"] for m in page1["items"]] == [4, 3] and page1["next_cursor"]
    page2 = client.get(url + f"&cursor={page1['next_cursor']}", headers=viewer_headers).json()
    assert [m["seq"] for m in page2["items"]] == [2, 1]
    page3 = client.get(url + f"&cursor={page2['next_cursor']}", headers=viewer_headers).json()
    assert [m["seq"] for m in page3["items"]] == [0] and page3["next_cursor"] is None
    assert client.get(f"{API}/stations/1/measurements?limit=1001", headers=viewer_headers).status_code == 422


def test_aggregate_hour_and_vector_mean(client, send, viewer_headers):
    start = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    send(seq=1, received_at=start, temperature_raw=2000, wind_direction_raw=350)
    send(seq=2, received_at=start + timedelta(minutes=20), temperature_raw=2200, wind_direction_raw=10)
    send(seq=3, received_at=start + timedelta(minutes=40), flags=FLAG_FAULT_DHT22, wind_direction_raw=0)
    send(seq=4, received_at=start + timedelta(hours=1, minutes=5), temperature_raw=3000, wind_direction_raw=90)
    r = client.get(f"{API}/stations/1/measurements/aggregate?from={iso(start)}&to={iso(start + timedelta(hours=2))}&bucket=hour",
                   headers=viewer_headers).json()
    assert len(r["items"]) == 2
    first = r["items"][0]
    assert first["bucket_start"] == "2026-09-01T12:00:00.000Z" and first["count"] == 3
    assert first["temperature_c"] == {"min": 20.0, "avg": 21.0, "max": 22.0, "count": 2}
    assert first["wind_direction_deg"] == {"avg": 0, "count": 3}
    assert r["items"][1]["temperature_c"]["max"] == 30.0


def test_aggregate_day_uses_local_day(client, send, viewer_headers):
    # 02:00 UTC = 23:00 do dia anterior em São Paulo
    send(seq=1, received_at=datetime(2026, 9, 2, 2, 0, tzinfo=timezone.utc))
    send(seq=2, received_at=datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc))
    r = client.get(f"{API}/stations/1/measurements/aggregate?from=2026-08-31T00:00:00Z&to=2026-09-04T00:00:00Z&bucket=day",
                   headers=viewer_headers).json()
    assert [i["bucket_start"] for i in r["items"]] == ["2026-09-01T03:00:00.000Z", "2026-09-02T03:00:00.000Z"]


def test_availability(client, send, viewer_headers):
    start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    for i in range(0, 6, 2):  # 3 de 6 esperadas em 1 h
        send(seq=i, received_at=start + timedelta(minutes=10 * i))
    r = client.get(f"{API}/stations/1/availability?from={iso(start)}&to={iso(start + timedelta(hours=1))}",
                   headers=viewer_headers).json()
    assert (r["expected"], r["received"], r["availability_pct"]) == (6, 3, 50.0)


def test_csv_export(client, send, viewer_headers):
    start = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
    send(seq=1, received_at=start, temperature_raw=-125)
    send(seq=2, received_at=start + timedelta(minutes=10), flags=FLAG_FAULT_DHT22)
    r = client.get(f"{API}/stations/1/measurements.csv?from={iso(start)}&to={iso(start + timedelta(hours=1))}",
                   headers=viewer_headers)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.content.decode("utf-8-sig").strip().split("\r\n")
    assert lines[0].startswith("Data e hora (horário de Brasília);Horário estimado;Temperatura (°C)")
    assert lines[1].startswith("01/09/2026 12:00:00;não;-1,3;64,2;923,5;3,1;135;41;6,3;3,99")
    assert lines[2].startswith("01/09/2026 12:10:00;não;;;923,5")
    assert len(lines) == 3


def test_period_validation(client, viewer_headers):
    r = client.get(f"{API}/stations/1/measurements?from=2026-09-02T00:00:00Z&to=2026-09-01T00:00:00Z", headers=viewer_headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_period"


# ---------------------------------------------------------------------------
# Administração
# ---------------------------------------------------------------------------

def test_gateway_admin(client, admin_headers):
    r = client.post(f"{API}/gateways", headers=admin_headers, json={"name": "Gateway campo"})
    assert r.status_code == 201
    created = r.json()
    assert len(created["secret_hex"]) == 32
    listed = client.get(f"{API}/gateways", headers=admin_headers).json()["items"]
    assert all("secret_hex" not in g and "secret_key" not in g for g in listed)
    rotated = client.post(f"{API}/gateways/{created['id']}/rotate-secret", headers=admin_headers).json()
    assert rotated["secret_hex"] != created["secret_hex"]

    body = json.dumps(make_payload()).encode()
    old = signed_headers(body, gateway_id=created["id"], secret=bytes.fromhex(created["secret_hex"]))
    assert client.post(f"{API}/ingest/measurements", content=body, headers=old).status_code == 401
    new = signed_headers(body, gateway_id=created["id"], secret=bytes.fromhex(rotated["secret_hex"]))
    assert client.post(f"{API}/ingest/measurements", content=body, headers=new).status_code == 201


def test_user_admin(client, admin_headers):
    r = client.post(f"{API}/users", headers=admin_headers,
                    json={"name": "Ana", "email": "Ana@Sitio.com", "password": "senha-forte-1"})
    assert r.status_code == 201 and r.json()["email"] == "ana@sitio.com" and r.json()["role"] == "viewer"
    uid = r.json()["id"]
    assert client.post(f"{API}/users", headers=admin_headers,
                       json={"name": "A", "email": "ana@sitio.com", "password": "senha-forte-1"}).status_code == 409
    assert client.patch(f"{API}/users/{uid}", headers=admin_headers, json={"is_active": False}).json()["is_active"] is False
    assert client.post(f"{API}/auth/login", json={"email": "ana@sitio.com", "password": "senha-forte-1"}).status_code == 401
    admin_id = client.get(f"{API}/auth/me", headers=admin_headers).json()["id"]
    r = client.patch(f"{API}/users/{admin_id}", headers=admin_headers, json={"role": "viewer"})
    assert r.status_code == 400


@pytest.mark.parametrize("path", ["/stations", "/alarm-events", "/stations/1/latest"])
def test_requires_auth(client, path):
    assert client.get(f"{API}{path}").status_code == 401


def test_gateway_secret_fixture_matches():
    assert len(GATEWAY_SECRET) == 16
