"""Tarefas periódicas: estações offline e limpeza de nonces (a cada 5 min)."""

import logging
import threading

from app.config import settings
from app.db.pool import connection
from app.services import alarms
from app.services.ingest import cleanup_nonces
from app.timeutil import utcnow

log = logging.getLogger(__name__)

PERIOD_S = 300


def run_once() -> None:
    with connection() as conn:
        opened = alarms.check_offline_stations(conn, utcnow(), settings.offline_factor)
        conn.commit()
        alarms.notify_opened(opened)
        cleanup_nonces(conn)


class PeriodicTasks:
    def __init__(self, period_s: int = PERIOD_S):
        self.period_s = period_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                run_once()
            except Exception:  # noqa: BLE001 - a tarefa continua no próximo ciclo
                log.exception("Falha na tarefa periódica")
            self._stop.wait(self.period_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="meteo10-periodic", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
