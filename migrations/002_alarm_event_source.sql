-- Identifica o sensor nos eventos de falha (um evento por sensor: dht22, bmp280, wind, soil, uv)
-- e acelera a busca de eventos abertos por regra.
ALTER TABLE alarm_events
  ADD COLUMN source VARCHAR(30) NULL AFTER type,
  ADD INDEX idx_evt_rule_open (rule_id, resolved_at),
  ADD INDEX idx_evt_started (started_at);

ALTER TABLE ingest_nonces
  ADD INDEX idx_nonce_created (created_at);
