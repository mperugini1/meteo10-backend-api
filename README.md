# meteo10-backend-api

Backend da plataforma web do **Meteo10** (PCS3858 – Laboratório de Sistemas Embarcados, Poli-USP): recebe as medições
encaminhadas pelo gateway LoRa, armazena no MySQL, avalia alarmes e expõe uma API REST para o frontend.

- Python 3.12+, FastAPI + Uvicorn, Pydantic v2
- MySQL 8.4 com `mysql-connector-python` — **SQL puro com pool de conexões, sem ORM**
- JWT (PyJWT) + PBKDF2-HMAC-SHA256 para usuários; **AES-CMAC** (`cryptography`) para o gateway
- Migrações em `migrations/NNN_*.sql`, aplicadas por `python -m app.db.migrate`

## Como rodar

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # ajuste JWT_SECRET e SEED_ADMIN_PASSWORD

docker compose up -d            # MySQL 8.4 (porta MYSQL_HOST_PORT, padrão 3306)
python -m app.db.migrate
python -m app.db.seed           # imprime o segredo do gateway de demonstração UMA vez
uvicorn app.main:app --reload
pytest
```

Documentação interativa: <http://localhost:8000/docs> (desativada com `APP_ENV=production`).

### Sem Docker (MySQL local)

Se já houver um MySQL na máquina, crie usuário e bancos uma vez com
`sudo mysql < setup_local_mysql.sql` e mantenha `DB_PORT=3306` no `.env`.
Para usar o compose ao lado de um MySQL local, defina `MYSQL_HOST_PORT=3307` e `DB_PORT=3307`.

### Testes

`pytest` roda os testes unitários sempre. Os testes de API usam o banco `meteo10_test`
(`TEST_DB_NAME` para outro nome), que é **apagado e recriado** a cada execução; sem acesso a ele, esses testes são pulados.

## Simulador de gateway

Envia medições assinadas com dados realistas de São Paulo (ciclo diário de temperatura e umidade, pressão 920–930 hPa,
vento com rajadas e direção predominante SE, bateria carregando de dia):

```bash
# ao vivo, a cada 5 s
python -m app.tools.simulate_gateway --gateway-id 1 --secret-hex <SEGREDO> --station-id 1

# 7 dias de histórico (intervalo da estação: 600 s) e depois para
python -m app.tools.simulate_gateway --gateway-id 1 --secret-hex <SEGREDO> --backfill-days 7 --count 0

# cenários: sensor-fault | low-battery | offline-gap | duplicates | retransmissions
python -m app.tools.simulate_gateway --gateway-id 1 --secret-hex <SEGREDO> --scenario low-battery --interval 2 --count 20
```

O último `seq` enviado por estação fica em `.sim_state.json`, para que execuções seguidas não sejam vistas como duplicatas.

## Contrato de ingestão (gateway → backend)

`POST /api/v1/ingest/measurements`, corpo JSON com os **valores brutos** do pacote LoRa (a conversão é feita só aqui):

```
X-Gateway-Id: 1
X-Timestamp: 1791043200                       # epoch UTC em segundos; janela de ±300 s
X-Nonce: 9f2c4a1e0b7d4c3a8e6f1a2b3c4d5e6f      # 16 bytes aleatórios em hex, uso único
X-Signature: <hex minúsculo>                   # AES-CMAC(secret_key, mensagem)

mensagem = "{gateway_id}\n{timestamp}\n{nonce}\n{sha256_hex(corpo_bruto)}"
```

Respostas: `201 created` · `200 duplicate` (mesmo `station_id`+`seq` em 24 h — reenvio é seguro) · `401` assinatura,
janela ou nonce inválidos · `404 station_not_found` · `422` corpo inválido. Um teste com o vetor da RFC 4493 garante a
compatibilidade do CMAC com o firmware.

## Decisões de implementação

Pontos que a especificação deixava em aberto, combinados com o time:

- **Retransmissões** (bit 1 de `flags`): gravadas com `measured_at` estimado; atualizam apenas `last_seen_at`.
  `last_seq`, `last_battery_mv` e a avaliação de alarmes só mudam com a medição mais recente (pacote normal).
- **Alarmes do sistema**: bateria baixa (< 3,3 V, alerta) e crítica (< 3,0 V) abrem eventos separados e fecham
  com histerese de 100 mV (≥ 3,4 V e ≥ 3,1 V). Falha de sensor gera um evento por sensor (`dht22`, `bmp280`, `wind`,
  `soil`, `uv`, na coluna `alarm_events.source`, migração 002) após 3 pacotes seguidos e fecha no primeiro pacote sem
  falha. Estação offline fecha quando chega nova medição.
- **Regras padrão no seed**: geada (temperatura < 2 °C) e calor (> 35 °C), histerese de 1 °C.
- A assinatura CMAC é conferida **antes** de gravar o nonce, para que requisições não autenticadas não escrevam no banco.
- Estação desativada é tratada como desconhecida na ingestão (`404`).
- Desativar, alterar a condição ou remover uma regra encerra o evento aberto dela (o histórico é mantido).
- Agregação diária usa o dia civil de `America/Sao_Paulo`.
- Rate limit do login: 10 tentativas **malsucedidas** por IP a cada 15 min. Quando a requisição vem de um proxy
  confiável (`TRUSTED_PROXY_IPS`, por padrão o próprio servidor do Next), vale o IP do `X-Forwarded-For`.
- Tarefas periódicas (offline a cada 5 min, limpeza de nonces com mais de 1 h) rodam numa thread do processo da API;
  use um único worker do Uvicorn ou `BACKGROUND_TASKS=false` nos demais.
- Notificações externas: registre uma função com `app.services.alarms.register_listener` (ponto de extensão).
