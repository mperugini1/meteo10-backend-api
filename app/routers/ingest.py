from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.db.pool import connection
from app.services import alarms
from app.services.ingest import authenticate_gateway, parse_payload, store_measurement

router = APIRouter(prefix="/ingest", tags=["ingestão"])


def _ingest(headers: dict[str, str], body: bytes) -> JSONResponse:
    with connection() as conn:
        gateway_id = authenticate_gateway(conn, headers, body)
        payload, raw_json = parse_payload(body)
        result = store_measurement(conn, gateway_id, payload, raw_json)
    if result.status == "duplicate":
        return JSONResponse({"status": "duplicate"}, status_code=200)
    alarms.notify_opened(result.opened_events)
    return JSONResponse({"status": "created", "measurement_id": result.measurement_id}, status_code=201)


@router.post(
    "/measurements",
    status_code=201,
    summary="Recebe uma medição do gateway (assinada com AES-CMAC)",
    responses={200: {"description": "Duplicada"}, 401: {"description": "Assinatura inválida"}, 404: {"description": "Estação desconhecida"}},
)
async def ingest_measurement(request: Request) -> JSONResponse:
    # O hash da assinatura usa o corpo bruto, antes de qualquer parse do JSON.
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    return await run_in_threadpool(_ingest, headers, body)
