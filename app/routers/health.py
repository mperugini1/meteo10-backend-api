from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db.pool import connection, query_one

router = APIRouter(tags=["saúde"])


@router.get("/health")
def health() -> JSONResponse:
    try:
        with connection() as conn:
            query_one(conn, "SELECT 1 AS ok")
        return JSONResponse({"status": "ok", "db": "ok"})
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": "error", "db": "error"}, status_code=503)
