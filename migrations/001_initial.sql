-- Meteo10: esquema inicial (especificação, seção 5.2)
-- Datas em UTC, DATETIME(3).

CREATE TABLE IF NOT EXISTS schema_migrations (
  version     VARCHAR(50) PRIMARY KEY,
  applied_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE users (
  id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  name          VARCHAR(120) NOT NULL,
  email         VARCHAR(190) NOT NULL UNIQUE,
  password_hash CHAR(64)     NOT NULL,          -- hex do PBKDF2-HMAC-SHA256
  password_salt CHAR(32)     NOT NULL,          -- hex de 16 bytes aleatórios
  role          ENUM('admin','viewer') NOT NULL DEFAULT 'viewer',
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE gateways (
  id           INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  name         VARCHAR(120) NOT NULL,
  secret_key   BINARY(16)   NOT NULL,           -- chave AES-128 para CMAC
  is_active    BOOLEAN NOT NULL DEFAULT TRUE,
  last_seen_at DATETIME(3) NULL,
  created_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE stations (
  id                     TINYINT UNSIGNED PRIMARY KEY,      -- igual ao station_id do pacote (1–255)
  name                   VARCHAR(120) NOT NULL,
  description            VARCHAR(500) NULL,
  latitude               DECIMAL(9,6) NULL,
  longitude              DECIMAL(9,6) NULL,
  altitude_m             SMALLINT NULL,
  measurement_interval_s INT UNSIGNED NOT NULL DEFAULT 600,
  is_active              BOOLEAN NOT NULL DEFAULT TRUE,
  last_seen_at           DATETIME(3) NULL,
  last_seq               SMALLINT UNSIGNED NULL,
  last_battery_mv        SMALLINT UNSIGNED NULL,
  created_at             DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE measurements (
  id                    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  station_id            TINYINT UNSIGNED NOT NULL,
  gateway_id            INT UNSIGNED NOT NULL,
  seq                   SMALLINT UNSIGNED NOT NULL,
  flags                 TINYINT UNSIGNED NOT NULL,
  measured_at           DATETIME(3) NOT NULL,
  measured_at_estimated BOOLEAN NOT NULL DEFAULT FALSE,
  received_at           DATETIME(3) NOT NULL,
  temperature_c         DECIMAL(5,2) NULL,
  humidity_pct          DECIMAL(5,2) NULL,
  pressure_hpa          DECIMAL(6,1) NULL,
  wind_speed_ms         DECIMAL(5,2) NULL,
  wind_direction_deg    SMALLINT UNSIGNED NULL,
  soil_moisture_pct     TINYINT UNSIGNED NULL,
  uv_index              DECIMAL(3,1) NULL,
  battery_v             DECIMAL(4,3) NULL,
  rssi_dbm              SMALLINT NULL,
  snr_db                DECIMAL(4,1) NULL,
  quality_flags         JSON NULL,                -- ex.: {"out_of_range": ["pressure"]}
  raw_payload           JSON NOT NULL,            -- corpo bruto recebido do gateway
  CONSTRAINT fk_meas_station FOREIGN KEY (station_id) REFERENCES stations(id),
  CONSTRAINT fk_meas_gateway FOREIGN KEY (gateway_id) REFERENCES gateways(id),
  INDEX idx_meas_station_time (station_id, measured_at),
  INDEX idx_meas_station_seq_recv (station_id, seq, received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE alarm_rules (
  id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  station_id  TINYINT UNSIGNED NOT NULL,
  variable    ENUM('temperature_c','humidity_pct','pressure_hpa','wind_speed_ms',
                   'soil_moisture_pct','uv_index','battery_v') NOT NULL,
  operator    ENUM('gt','lt') NOT NULL,
  threshold   DECIMAL(8,2) NOT NULL,
  hysteresis  DECIMAL(8,2) NOT NULL DEFAULT 0,
  is_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
  created_by  INT UNSIGNED NOT NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  CONSTRAINT fk_rule_station FOREIGN KEY (station_id) REFERENCES stations(id) ON DELETE CASCADE,
  CONSTRAINT fk_rule_user FOREIGN KEY (created_by) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE alarm_events (
  id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  station_id       TINYINT UNSIGNED NOT NULL,
  rule_id          INT UNSIGNED NULL,             -- NULL para alarmes do sistema
  type             ENUM('rule','battery_low','battery_critical','station_offline','sensor_fault') NOT NULL,
  severity         ENUM('info','warning','critical') NOT NULL,
  message          VARCHAR(300) NOT NULL,         -- texto em português para a interface
  trigger_value    DECIMAL(8,2) NULL,
  measurement_id   BIGINT UNSIGNED NULL,
  started_at       DATETIME(3) NOT NULL,
  resolved_at      DATETIME(3) NULL,
  acknowledged_at  DATETIME(3) NULL,
  acknowledged_by  INT UNSIGNED NULL,
  CONSTRAINT fk_evt_station FOREIGN KEY (station_id) REFERENCES stations(id) ON DELETE CASCADE,
  CONSTRAINT fk_evt_rule FOREIGN KEY (rule_id) REFERENCES alarm_rules(id) ON DELETE SET NULL,
  INDEX idx_evt_open (station_id, resolved_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE ingest_nonces (                      -- proteção contra repetição (seção 7.2)
  gateway_id  INT UNSIGNED NOT NULL,
  nonce       CHAR(32) NOT NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (gateway_id, nonce)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
