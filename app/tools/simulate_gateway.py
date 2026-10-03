"""Simulador de gateway LoRa: envia medições assinadas (AES-CMAC) para /ingest/measurements.

Exemplos:
  python -m app.tools.simulate_gateway --gateway-id 1 --secret-hex <hex> --station-id 1
  python -m app.tools.simulate_gateway --gateway-id 1 --secret-hex <hex> --backfill-days 7 --count 0
  python -m app.tools.simulate_gateway ... --scenario sensor-fault --interval 2 --count 10
"""

import argparse
import json
import math
import random
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.security.gateway_signature import sign
from app.services.processing import FLAG_FAULT_DHT22, FLAG_RETRANSMITTED, SEQ_MODULO

SCENARIOS = ("sensor-fault", "low-battery", "offline-gap", "duplicates", "retransmissions")
STATE_FILE = Path(".sim_state.json")
SP_OFFSET = timedelta(hours=-3)  # America/Sao_Paulo (sem horário de verão)


# ---------------------------------------------------------------------------
# Modelo de clima de São Paulo
# ---------------------------------------------------------------------------

def _noise(t: datetime, salt: int, scale: float) -> float:
    rng = random.Random(int(t.timestamp()) // 60 * 1000 + salt)
    return rng.uniform(-scale, scale)


def _local_hour(t: datetime) -> float:
    local = t + SP_OFFSET
    return local.hour + local.minute / 60


def _day_index(t: datetime) -> float:
    return t.timestamp() / 86400


def weather(t: datetime) -> dict[str, float]:
    """Valores físicos realistas para o instante t (UTC)."""
    h = _local_hour(t)
    day = _day_index(t)
    daily = math.sin(2 * math.pi * (h - 9) / 24)  # máximo às 15 h, mínimo às 3 h
    day_shift = 2.0 * math.sin(2 * math.pi * day / 5.3)  # frentes de alguns dias

    temperature = 22 + 7 * daily + day_shift + _noise(t, 1, 0.4)
    temperature = max(14.0, min(30.0, temperature))
    humidity = 67.5 - 26 * daily - 3 * day_shift + _noise(t, 2, 1.5)
    humidity = max(40.0, min(95.0, humidity))
    pressure = 925 + 3.5 * math.sin(2 * math.pi * day / 3.1) + 0.8 * math.sin(4 * math.pi * h / 24) + _noise(t, 3, 0.1)
    pressure = max(920.0, min(930.0, pressure))

    base_wind = 2.5 + 1.5 * math.sin(2 * math.pi * (h - 11) / 24) + 0.8 * math.sin(2 * math.pi * day / 2.7)
    gust = 3.5 if random.Random(int(t.timestamp()) // 600 + 4).random() < 0.07 else 0.0
    wind_speed = max(0.0, min(8.0, base_wind + gust + _noise(t, 5, 0.5)))
    wind_direction = (135 + 35 * math.sin(2 * math.pi * day / 4) + _noise(t, 6, 25)) % 360  # predominante SE

    soil = 45 + 12 * math.cos(2 * math.pi * (day % 3) / 3) + _noise(t, 7, 1)  # irrigação a cada 3 dias
    uv = max(0.0, 11 * math.sin(math.pi * (h - 6) / 12)) if 6 < h < 18 else 0.0
    uv = max(0.0, uv + _noise(t, 8, 0.3)) if uv > 0 else 0.0

    # Bateria: carrega com o sol (até ~16 h), descarrega à noite
    if 7 <= h <= 16:
        battery = 3.75 + 0.40 * (h - 7) / 9
    elif h > 16:
        battery = 4.15 - 0.025 * (h - 16)
    else:
        battery = 4.15 - 0.025 * (h + 8)
    battery += _noise(t, 9, 0.01)

    return {
        "temperature": temperature,
        "humidity": humidity,
        "pressure": pressure,
        "wind_speed": wind_speed,
        "wind_direction": wind_direction,
        "soil": max(0.0, min(100.0, soil)),
        "uv": min(20.0, uv),
        "battery": max(2.6, min(4.2, battery)),
    }


def to_raw(values: dict[str, float]) -> dict[str, int]:
    return {
        "temperature_raw": int(round(values["temperature"] * 100)),
        "humidity_raw": int(round(values["humidity"] * 100)),
        "pressure_raw": int(round(values["pressure"] * 10)),
        "wind_speed_raw": int(round(values["wind_speed"] * 100)),
        "wind_direction_raw": int(round(values["wind_direction"])) % 360,
        "soil_moisture_raw": int(round(values["soil"])),
        "uv_index_raw": int(round(values["uv"] * 10)),
        "battery_mv": int(round(values["battery"] * 1000)),
    }


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------

@dataclass
class Packet:
    station_id: int
    seq: int
    flags: int
    raw: dict[str, int]
    received_at: datetime
    rssi_dbm: int
    snr_db: float

    def body(self) -> bytes:
        payload = {
            "station_id": self.station_id,
            "seq": self.seq,
            "flags": self.flags,
            **self.raw,
            "rssi_dbm": self.rssi_dbm,
            "snr_db": self.snr_db,
            "received_at": self.received_at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.received_at.microsecond // 1000:03d}Z",
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class Gateway:
    def __init__(self, base_url: str, gateway_id: int, secret: bytes, verbose: bool = True):
        self.url = base_url.rstrip("/") + "/ingest/measurements"
        self.gateway_id = gateway_id
        self.secret = secret
        self.verbose = verbose
        self.client = httpx.Client(timeout=10)

    def send(self, packet: Packet, label: str = "") -> httpx.Response:
        body = packet.body()
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        headers = {
            "Content-Type": "application/json",
            "X-Gateway-Id": str(self.gateway_id),
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": sign(self.secret, self.gateway_id, timestamp, nonce, body),
        }
        response = self.client.post(self.url, content=body, headers=headers)
        if self.verbose:
            local = (packet.received_at + SP_OFFSET).strftime("%d/%m/%Y %H:%M")
            print(f"seq={packet.seq:5d} flags={packet.flags:#04x} {local} -> {response.status_code} {response.text}{label}")
        return response


def load_seq(station_id: int, override: int | None) -> int:
    if override is not None:
        return override % SEQ_MODULO
    try:
        state = json.loads(STATE_FILE.read_text())
        return (int(state.get(str(station_id), -1)) + 1) % SEQ_MODULO
    except (OSError, ValueError):
        return random.randrange(SEQ_MODULO)


def save_seq(station_id: int, seq: int) -> None:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    state[str(station_id)] = seq
    STATE_FILE.write_text(json.dumps(state))


def make_packet(station_id: int, seq: int, t: datetime, scenario: str | None, index: int, total: int | None) -> Packet:
    values = weather(t)
    flags = 0
    if scenario == "sensor-fault" and index % 10 < 4:
        flags |= FLAG_FAULT_DHT22  # falha em 4 de cada 10 pacotes seguidos
    if scenario == "low-battery":
        span = total or 20
        values["battery"] = 3.45 - 0.55 * min(1.0, index / max(1, span - 1))  # 3,45 V -> 2,90 V
    rng = random.Random(seq)
    return Packet(
        station_id=station_id, seq=seq, flags=flags, raw=to_raw(values), received_at=t,
        rssi_dbm=rng.randint(-112, -85), snr_db=round(rng.uniform(-2, 9), 1),
    )


def run(args: argparse.Namespace) -> int:
    try:
        secret = bytes.fromhex(args.secret_hex)
    except ValueError:
        secret = b""
    if len(secret) != 16:
        print(
            "Segredo inválido: use os 32 caracteres hexadecimais impressos pelo seed"
            " (ou gere um novo em Administração → Gateways).",
            file=sys.stderr,
        )
        return 2
    gateway = Gateway(args.url, args.gateway_id, secret)
    seq = load_seq(args.station_id, args.start_seq)
    index = 0

    def step(packet: Packet, label: str = "") -> None:
        gateway.send(packet, label)
        if args.scenario == "duplicates":
            gateway.send(packet, "  (reenvio duplicado)")

    # Histórico retroativo, respeitando o intervalo da estação
    if args.backfill_days > 0:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start = now - timedelta(days=args.backfill_days)
        total = int((now - start).total_seconds() // args.station_interval)
        gap_start = now - timedelta(days=1, hours=3)
        gap_end = now - timedelta(days=1)
        print(f"Gerando {total} medições retroativas ({args.backfill_days} dias)...")
        gateway.verbose = False
        created = 0
        for i in range(total):
            t = start + timedelta(seconds=i * args.station_interval)
            if args.scenario == "offline-gap" and gap_start <= t < gap_end:
                seq = (seq + 1) % SEQ_MODULO
                continue
            response = gateway.send(make_packet(args.station_id, seq, t, args.scenario, i, total))
            if response.status_code == 201:
                created += 1
            elif response.status_code not in (200,):
                print(f"Erro no seq={seq}: {response.status_code} {response.text}", file=sys.stderr)
                return 1
            seq = (seq + 1) % SEQ_MODULO
            if i % 200 == 0:
                print(f"  {i}/{total}")
        save_seq(args.station_id, (seq - 1) % SEQ_MODULO)
        print(f"Histórico concluído: {created} medições criadas.")
        gateway.verbose = True

    pending: list[Packet] = []
    try:
        while args.count == -1 or index < args.count:
            t = datetime.now(timezone.utc)
            packet = make_packet(args.station_id, seq, t, args.scenario, index, args.count if args.count > 0 else None)
            if args.scenario == "retransmissions" and index % 5 == 2:
                pending.append(packet)  # "falha de envio": fica no SD para retransmissão
                print(f"seq={seq:5d} não enviado (guardado para retransmissão)")
            else:
                step(packet)
                for old in pending:  # nó retransmite pendências depois da medição atual
                    old.flags |= FLAG_RETRANSMITTED
                    old.received_at = datetime.now(timezone.utc)
                    step(old, "  (retransmissão)")
                pending.clear()
            save_seq(args.station_id, seq)
            seq = (seq + 1) % SEQ_MODULO
            index += 1
            if args.count != -1 and index >= args.count:
                break
            wait = args.interval
            if args.scenario == "offline-gap" and index == 3:
                wait = args.gap_seconds
                print(f"Simulando estação sem comunicação por {wait} s...")
            time.sleep(wait)
    except KeyboardInterrupt:
        print("\nSimulação encerrada.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulador de gateway LoRa do Meteo10")
    parser.add_argument("--url", default="http://localhost:8000/api/v1", help="URL base da API")
    parser.add_argument("--gateway-id", type=int, required=True)
    parser.add_argument("--secret-hex", required=True, help="Segredo AES-128 do gateway (32 hex)")
    parser.add_argument("--station-id", type=int, default=1)
    parser.add_argument("--interval", type=float, default=5, help="Segundos entre envios (padrão 5)")
    parser.add_argument("--count", type=int, default=-1, help="Quantidade de envios ao vivo (-1 = infinito, 0 = nenhum)")
    parser.add_argument("--backfill-days", type=float, default=0, help="Gera histórico retroativo de N dias")
    parser.add_argument("--station-interval", type=int, default=600, help="Intervalo da estação para o histórico (s)")
    parser.add_argument("--start-seq", type=int, default=None, help="Número de sequência inicial")
    parser.add_argument("--scenario", choices=SCENARIOS, default=None)
    parser.add_argument("--gap-seconds", type=int, default=1860, help="Duração da pausa no cenário offline-gap")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
