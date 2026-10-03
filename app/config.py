"""Configuração da aplicação lida de variáveis de ambiente (.env)."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


@dataclass
class Settings:
    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    db_host: str = field(default_factory=lambda: os.getenv("DB_HOST", "127.0.0.1"))
    db_port: int = field(default_factory=lambda: int(os.getenv("DB_PORT", "3306")))
    db_name: str = field(default_factory=lambda: os.getenv("DB_NAME", "meteo10"))
    db_user: str = field(default_factory=lambda: os.getenv("DB_USER", "meteo10"))
    db_password: str = field(default_factory=lambda: os.getenv("DB_PASSWORD", "meteo10"))
    db_pool_size: int = field(default_factory=lambda: int(os.getenv("DB_POOL_SIZE", "5")))
    jwt_secret: str = field(default_factory=lambda: os.getenv("JWT_SECRET", "troque-este-segredo"))
    jwt_expires_minutes: int = field(default_factory=lambda: int(os.getenv("JWT_EXPIRES_MINUTES", "480")))
    pbkdf2_iterations: int = field(default_factory=lambda: int(os.getenv("PBKDF2_ITERATIONS", "600000")))
    frontend_origin: str = field(default_factory=lambda: os.getenv("FRONTEND_ORIGIN", "http://localhost:3000"))
    sentry_dsn: str = field(default_factory=lambda: os.getenv("SENTRY_DSN", ""))
    seed_admin_email: str = field(default_factory=lambda: os.getenv("SEED_ADMIN_EMAIL", "admin@meteo10.local"))
    seed_admin_password: str = field(default_factory=lambda: os.getenv("SEED_ADMIN_PASSWORD", "troque-esta-senha"))
    seed_gateway_secret_hex: str = field(default_factory=lambda: os.getenv("SEED_GATEWAY_SECRET_HEX", ""))
    offline_factor: int = field(default_factory=lambda: int(os.getenv("OFFLINE_FACTOR", "3")))
    trusted_proxy_ips: list[str] = field(default_factory=lambda: _list("TRUSTED_PROXY_IPS", "127.0.0.1,::1"))
    background_tasks: bool = field(default_factory=lambda: _bool("BACKGROUND_TASKS", True))

    # Parâmetros fixos do protocolo de ingestão (seção 7.2)
    ingest_time_window_s: int = 300
    nonce_retention_s: int = 3600

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


settings = Settings()
