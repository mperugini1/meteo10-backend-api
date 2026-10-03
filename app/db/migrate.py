"""Aplica as migrações SQL numeradas de migrations/ em ordem.

Uso: python -m app.db.migrate
"""

import re
import sys
from pathlib import Path

from app.db.pool import raw_connection

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_FILE_RE = re.compile(r"^(\d{3})_[\w-]+\.sql$")


def split_statements(sql: str) -> list[str]:
    """Separa comandos por ';' no fim da linha, ignorando comentários de linha."""
    statements: list[str] = []
    current: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not current and (stripped == "" or stripped.startswith("--")):
            continue
        current.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
    tail = "\n".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def migration_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.iterdir() if _FILE_RE.match(p.name))


def migrate(database: str | None = None, verbose: bool = True) -> list[str]:
    conn = raw_connection(database)
    applied_now: list[str] = []
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version VARCHAR(50) PRIMARY KEY,"
            " applied_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3))"
            " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
        )
        cur.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}
        for path in migration_files():
            version = path.stem
            if version in applied:
                continue
            if verbose:
                print(f"Aplicando {path.name}...")
            for statement in split_statements(path.read_text(encoding="utf-8")):
                cur.execute(statement)
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            conn.commit()
            applied_now.append(version)
        cur.close()
    finally:
        conn.close()
    if verbose:
        print("Banco atualizado." if applied_now else "Nenhuma migração pendente.")
    return applied_now


if __name__ == "__main__":
    try:
        migrate()
    except Exception as exc:  # noqa: BLE001 - mensagem amigável na linha de comando
        print(f"Erro ao aplicar migrações: {exc}", file=sys.stderr)
        sys.exit(1)
