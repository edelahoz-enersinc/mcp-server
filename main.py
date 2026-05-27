import asyncio
import json
import logging
import os
import sys
from collections import deque
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode
from uuid import UUID

import httpx
import mcp.types as types
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

load_dotenv()

logger = logging.getLogger(__name__)

API_KEY = os.getenv("API_KEY_AMBIENTES")
API_BASE_URL = os.getenv("API_URL_BASE", "").rstrip("/")

GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
GEMINI_AGENT_LOCATION = os.getenv("GEMINI_AGENT_LOCATION", "").strip()
GEMINI_AGENT_ID = os.getenv("GEMINI_AGENT_ID", "").strip()

_gemini_reasoning_engine: Any | None = None
_gemini_init_started = False


def _tool_text_for_mcp(text: str) -> str:
    """
    Normaliza texto para respuestas MCP: puntuación tipográfica que a veces rompe
    serializadores estrictos (p. ej. U+2026 …). El cuerpo sigue en UTF-8 (español).
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return (
        text.replace("\u2026", "...")
        .replace("\u2013", "-")
        .replace("\u2014", "--")
        .replace("\u00a0", " ")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
# Si es true, no se infiere session_id por heurística (modo multi-cliente / producción).
MCP_SSE_STRICT_SESSION = os.getenv("MCP_SSE_STRICT_SESSION", "").lower() in ("1", "true", "yes")
# Streamable HTTP sin estado: cada POST es independiente (recomendado en Cloud Run multi-réplica).
MCP_STREAMABLE_STATELESS = os.getenv("MCP_STREAMABLE_STATELESS", "1").lower() in ("1", "true", "yes")
# Logs estructurados para diagnosticar 400/404 en POST MCP (no activar en producción con tráfico alto).
# Comprueba en tiempo de ejecución (p. ej. tras load_dotenv / vars de Cloud Run).
def _mcp_is_debug() -> bool:
    return os.getenv("MCP_DEBUG", "").strip().lower() in ("1", "true", "yes")


_MCP_LOGGING_READY = False
_MCP_BOOT_LINE_PRINTED = False


def _ensure_mcp_logger_emits_debug() -> None:
    """Uvicorn a veces deja el root en WARNING; añadimos handler propio al logger `main`."""
    global _MCP_LOGGING_READY
    if _MCP_LOGGING_READY:
        return
    _MCP_LOGGING_READY = True
    if not _mcp_is_debug():
        return
    lg = logging.getLogger(__name__)
    lg.setLevel(logging.DEBUG)
    if lg.handlers:
        return
    h = logging.StreamHandler(sys.stderr)
    h.setLevel(logging.DEBUG)
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    lg.addHandler(h)
    lg.propagate = False


def _print_mcp_boot_once() -> None:
    """Siempre va a stdout (Cloud Run); sin secretos. Comprueba que las vars llegaron al proceso."""
    global _MCP_BOOT_LINE_PRINTED
    if _MCP_BOOT_LINE_PRINTED:
        return
    _MCP_BOOT_LINE_PRINTED = True
    raw_debug = os.getenv("MCP_DEBUG")
    strict = os.getenv("MCP_SSE_STRICT_SESSION")
    key_ok = bool((os.getenv("API_KEY_AMBIENTES") or "").strip())
    base = (os.getenv("API_URL_BASE") or "").strip()
    gemini_ok = bool(GOOGLE_CLOUD_PROJECT and GEMINI_AGENT_LOCATION and GEMINI_AGENT_ID)
    print(
        "[MCP-BOOT] "
        f"MCP_DEBUG_env={raw_debug!r} effective_debug={_mcp_is_debug()} "
        f"MCP_SSE_STRICT_SESSION={strict!r} "
        f"MCP_STREAMABLE_STATELESS={MCP_STREAMABLE_STATELESS} "
        f"API_KEY_AMBIENTES={'set' if key_ok else 'MISSING'} "
        f"API_URL_BASE_len={len(base)} "
        f"GEMINI_AGENT={'configured' if gemini_ok else 'MISSING_VARS'} "
        f"GOOGLE_CLOUD_PROJECT={'set' if GOOGLE_CLOUD_PROJECT else 'MISSING'} "
        f"GEMINI_AGENT_LOCATION={GEMINI_AGENT_LOCATION!r} "
        f"GEMINI_AGENT_ID_len={len(GEMINI_AGENT_ID)} "
        f"K_SERVICE={os.getenv('K_SERVICE', '')!r} K_REVISION={os.getenv('K_REVISION', '')!r}",
        flush=True,
    )
    if base:
        tail = base[-40:] if len(base) > 40 else base
        print(f"[MCP-BOOT] API_URL_BASE_suffix=...{tail}", flush=True)
    if not _mcp_is_debug():
        print(
            "[MCP-BOOT] MCP_DEBUG no está activo; añade MCP_DEBUG=1 en el servicio para logs [MCP-DEBUG]",
            flush=True,
        )


def _init_gemini_reasoning_engine() -> None:
    """
    Inicializa Vertex AI y el Reasoning Engine remoto (import lazy).

    No debe lanzar excepciones: un fallo aquí no debe impedir que Uvicorn abra el puerto.
    """
    global _gemini_reasoning_engine
    if not GOOGLE_CLOUD_PROJECT or not GEMINI_AGENT_LOCATION or not GEMINI_AGENT_ID:
        logger.warning(
            "Google Chat: faltan GOOGLE_CLOUD_PROJECT, GEMINI_AGENT_LOCATION o "
            "GEMINI_AGENT_ID; POST /google-chat no podrá invocar al agente."
        )
        return

    try:
        import vertexai
        from vertexai.preview import reasoning_engines

        vertexai.init(project=GOOGLE_CLOUD_PROJECT, location=GEMINI_AGENT_LOCATION)
        resource_name = (
            f"projects/{GOOGLE_CLOUD_PROJECT}/locations/{GEMINI_AGENT_LOCATION}"
            f"/reasoningEngines/{GEMINI_AGENT_ID}"
        )
        _gemini_reasoning_engine = reasoning_engines.ReasoningEngine(resource_name)
        logger.info(
            "Google Chat: Reasoning Engine listo en location=%s agent_id_len=%s",
            GEMINI_AGENT_LOCATION,
            len(GEMINI_AGENT_ID),
        )
    except Exception:
        _gemini_reasoning_engine = None
        logger.exception(
            "Google Chat: no se pudo inicializar Reasoning Engine; "
            "POST /google-chat responderá con error de configuración."
        )


def _schedule_gemini_init() -> None:
    """Arranca la init de Gemini en segundo plano tras abrir el puerto HTTP."""
    global _gemini_init_started
    if _gemini_init_started:
        return
    if not (GOOGLE_CLOUD_PROJECT and GEMINI_AGENT_LOCATION and GEMINI_AGENT_ID):
        return
    _gemini_init_started = True
    asyncio.create_task(asyncio.to_thread(_init_gemini_reasoning_engine))


def _extract_reasoning_engine_output(agent_response: Any) -> str:
    """Obtiene el texto de salida del agente (dict con 'output' u otros formatos)."""
    if agent_response is None:
        return ""
    if isinstance(agent_response, dict):
        output = agent_response.get("output")
        if output is not None:
            return str(output).strip()
        return json.dumps(agent_response, ensure_ascii=False)
    if hasattr(agent_response, "get"):
        output = agent_response.get("output")  # type: ignore[union-attr]
        if output is not None:
            return str(output).strip()
    return str(agent_response).strip()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _ensure_mcp_logger_emits_debug()
    _print_mcp_boot_once()
    logger.info(
        "[MCP-BOOT] logger=%s handlers=%s debug=%s streamable_stateless=%s",
        __name__,
        len(logger.handlers),
        _mcp_is_debug(),
        MCP_STREAMABLE_STATELESS,
    )
    async with _streamable_session_manager.run():
        _schedule_gemini_init()
        yield


_EMPTY = "(vacío)"


def _mcp_mask_token(value: str, head: int = 4, tail: int = 4) -> str:
    v = (value or "").strip()
    if not v:
        return _EMPTY
    if len(v) <= head + tail + 3:
        return "***"
    return f"{v[:head]}...{v[-tail:]}"


def _mcp_debug(message: str, **fields: Any) -> None:
    if not _mcp_is_debug():
        return
    _ensure_mcp_logger_emits_debug()
    parts = [f"{k}={v!r}" for k, v in sorted(fields.items())]
    logger.info("[MCP-DEBUG] %s | %s", message, " ".join(parts))

# Sesiones MCP abiertas por GET /mcp (más reciente al final). El SDK no borra entradas de
# `_read_stream_writers`, así que no basta con len(writers)==1 tras varias conexiones.
_RECENT_MCP_SESSION_HEX: deque[str] = deque(maxlen=64)


class AlreadyHandledResponse(Response):
    """
    `SseServerTransport.handle_post_message` y el flujo SSE de `connect_sse`
    ya emiten la respuesta HTTP vía ASGI (`send`). FastAPI invocaría de nuevo
    `Response.__call__` sobre el valor de retorno; esta subclase no envía nada.
    """

    def __init__(self) -> None:
        super().__init__(status_code=204)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        return


# --- Servidor MCP (herramientas y ejecución) ---
mcp_server = Server("gestor-ambientes-etrm")


@mcp_server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="consultar_estado_ambiente",
            description="Consulta si el ambiente está ENCENDIDO o APAGADO.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ambiente": {"type": "string", "description": "Ej: ETRM-QA"}
                },
                "required": ["ambiente"],
            },
        ),
        types.Tool(
            name="ejecutar_accion_ambiente",
            description="Envía la orden para ENCENDER o APAGAR un ambiente completo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ambiente": {"type": "string", "description": "Ej: ETRM-QA"},
                    "accion": {
                        "type": "string",
                        "enum": ["encender", "apagar"],
                        "description": "La acción a realizar",
                    },
                },
                "required": ["ambiente", "accion"],
            },
        ),
    ]


@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    if not arguments:
        return [types.TextContent(type="text", text="Error: Faltan argumentos.")]

    headers = {"x-api-key": API_KEY}

    if name == "consultar_estado_ambiente":
        ambiente = arguments.get("ambiente")
        url = f"{API_BASE_URL}/status/{ambiente}"
        try:
            async with httpx.AsyncClient(headers=headers) as client:
                response = await client.get(url, timeout=15.0)
                body = json.dumps(response.json(), ensure_ascii=False, separators=(",", ":"))
                return [types.TextContent(type="text", text=_tool_text_for_mcp(body))]
        except Exception as e:
            return [types.TextContent(type="text", text=_tool_text_for_mcp(f"Error al consultar: {e!s}"))]

    if name == "ejecutar_accion_ambiente":
        ambiente = arguments.get("ambiente")
        accion = arguments.get("accion")
        url = f"{API_BASE_URL}/control"
        payload = {"ambiente": ambiente, "accion": accion}

        try:
            async with httpx.AsyncClient(headers=headers) as client:
                response = await client.post(url, json=payload, timeout=15.0)
                response.raise_for_status()
                data = response.json()

            resumen = []
            for recurso, info in data.get("resultados", {}).items():
                raw = info.get("messages", ["Sin mensaje"])[-1]
                msg = raw if isinstance(raw, str) else str(raw)
                resumen.append(f"- {recurso}: {msg}")

            texto_final = f"Resultado de la acción {accion} en {ambiente}:\n" + "\n".join(resumen)
            return [types.TextContent(type="text", text=_tool_text_for_mcp(texto_final))]

        except Exception as e:
            return [types.TextContent(type="text", text=_tool_text_for_mcp(f"Error al ejecutar acción: {e!s}"))]

    return [types.TextContent(type="text", text="Herramienta no encontrada.")]


# --- FastAPI: aplicación y transporte SSE (MCP) ---
app = FastAPI(
    title="Gestor ambientes ETRM (MCP)",
    description=(
        "Servidor MCP (HTTP+SSE / Streamable HTTP) y webhook de Google Chat "
        "conectado a un Reasoning Engine de Vertex AI."
    ),
    lifespan=_lifespan,
)

# Mismo path que GET /mcp: el evento SSE `endpoint` será `/mcp?session_id=<hex>`.
sse = SseServerTransport("/mcp")

# Agent Platform / ADK usan Streamable HTTP en POST /mcp (sin session_id en query).
_streamable_session_manager = StreamableHTTPSessionManager(
    mcp_server,
    stateless=MCP_STREAMABLE_STATELESS,
)


def _is_legacy_sse_post(request: Request) -> bool:
    """POST con session_id en query → cliente MCP SSE (p. ej. prueba.py)."""
    return bool(request.query_params.get("session_id"))


def _is_streamable_http_request(request: Request) -> bool:
    """GET/DELETE con cabecera mcp-session-id, o POST sin session_id en query."""
    if request.headers.get(MCP_SESSION_ID_HEADER):
        return True
    if request.method in ("GET", "DELETE"):
        return False
    return not _is_legacy_sse_post(request)


def _normalize_session_token(value: str) -> str:
    sid = value.strip()
    if "-" in sid:
        try:
            return UUID(sid).hex
        except ValueError:
            pass
    return sid


def _scope_with_query_string(scope: Scope, params: dict[str, str]) -> Scope:
    merged = dict(scope)
    merged["query_string"] = urlencode(params).encode("latin-1")
    return merged


def _fallback_session_hex_for_post(writers: dict[UUID, Any]) -> str | None:
    """Elige sesión cuando el POST no trae `session_id` (heurística; desactivar con MCP_SSE_STRICT_SESSION)."""
    if len(writers) == 1:
        return next(iter(writers.keys())).hex
    for hx in reversed(_RECENT_MCP_SESSION_HEX):
        try:
            uid = UUID(hex=hx)
        except ValueError:
            continue
        if uid in writers:
            return hx
    return None


def _scope_for_mcp_post(request: Request) -> Scope:
    """
    El transporte SSE espera `session_id` en la query. Compatibilidad:
    - Cabecera `mcp-session-id` o `x-session-id` (hex o UUID).
    - Si `MCP_SSE_STRICT_SESSION` no está activado: sesión única en el transporte o la
      más reciente registrada por GET /mcp en este proceso (deque).
    """
    scope = request.scope
    raw_qs = scope.get("query_string") or b""
    try:
        params = dict(parse_qsl(raw_qs.decode("latin-1"), keep_blank_values=True))
    except ValueError:
        params = {}

    if params.get("session_id"):
        _mcp_debug(
            "POST session_id desde query",
            path=request.url.path,
            session_id_masked=_mcp_mask_token(str(params.get("session_id", ""))),
        )
        return scope

    header_sid = (
        (request.headers.get(MCP_SESSION_ID_HEADER) or "").strip()
        or (request.headers.get("x-session-id") or "").strip()
    )
    if header_sid:
        params["session_id"] = _normalize_session_token(header_sid)
        _mcp_debug(
            "POST session_id desde cabecera",
            path=request.url.path,
            header_masked=_mcp_mask_token(header_sid),
        )
        return _scope_with_query_string(scope, params)

    if MCP_SSE_STRICT_SESSION:
        writers = getattr(sse, "_read_stream_writers", None)
        wcount = len(writers) if isinstance(writers, dict) else -1
        _mcp_debug(
            "POST sin session_id y MCP_SSE_STRICT_SESSION activo (no hay heurística)",
            path=request.url.path,
            writers_count=wcount,
            recent_sessions=len(_RECENT_MCP_SESSION_HEX),
        )
        return scope

    writers = getattr(sse, "_read_stream_writers", None)
    if not isinstance(writers, dict) or not writers:
        _mcp_debug(
            "POST sin session_id y sin writers en esta instancia (¿POST en otro pod?)",
            path=request.url.path,
            writers_count=0,
            recent_sessions=len(_RECENT_MCP_SESSION_HEX),
        )
        return scope

    fb_hex = _fallback_session_hex_for_post(writers)
    if fb_hex:
        params["session_id"] = fb_hex
        logger.warning(
            "MCP POST sin session_id ni cabecera: usando heurística de sesión "
            "(última o única en este proceso)."
        )
        _mcp_debug(
            "POST session_id por heurística",
            path=request.url.path,
            writers_count=len(writers),
            chosen_masked=_mcp_mask_token(fb_hex),
            recent_sessions=len(_RECENT_MCP_SESSION_HEX),
        )
        return _scope_with_query_string(scope, params)

    _mcp_debug(
        "POST sin session_id y heurística sin coincidencia (deque vacío o UUIDs no en writers)",
        path=request.url.path,
        writers_count=len(writers),
        recent_sessions=len(_RECENT_MCP_SESSION_HEX),
    )
    return scope


async def _mcp_post_message(request: Request) -> Response:
    """Delegado ASGI para JSON-RPC; la respuesta real la emite el transporte MCP."""
    qs_in = (request.scope.get("query_string") or b"").decode("latin-1", errors="replace")
    _mcp_debug(
        "POST MCP entrada",
        path=request.url.path,
        query_string=qs_in or _EMPTY,
        content_type=request.headers.get("content-type") or _EMPTY,
        content_length=request.headers.get("content-length") or "?",
        has_mcp_session_id_header=bool(request.headers.get(MCP_SESSION_ID_HEADER)),
        has_x_session_id_header=bool(request.headers.get("x-session-id")),
        strict_session=MCP_SSE_STRICT_SESSION,
    )
    scope = _scope_for_mcp_post(request)
    qs_out = (scope.get("query_string") or b"").decode("latin-1", errors="replace")
    _mcp_debug(
        "POST MCP scope resuelto",
        path=request.url.path,
        query_after=qs_out or _EMPTY,
        session_injected=qs_in != qs_out,
    )
    await sse.handle_post_message(scope, request.receive, request._send)
    return AlreadyHandledResponse()


async def _handle_streamable_http(request: Request) -> Response:
    _mcp_debug(
        "Streamable HTTP",
        path=request.url.path,
        method=request.method,
        stateless=MCP_STREAMABLE_STATELESS,
        has_mcp_session_id_header=bool(request.headers.get(MCP_SESSION_ID_HEADER)),
    )
    await _streamable_session_manager.handle_request(
        request.scope, request.receive, request._send
    )
    return AlreadyHandledResponse()


@app.get("/mcp")
async def mcp_sse(request: Request) -> Response:
    """
    GET /mcp: Streamable HTTP (cabecera mcp-session-id) o SSE legacy (p. ej. prueba.py).
    """
    if _is_streamable_http_request(request):
        return await _handle_streamable_http(request)

    keys_before = frozenset(sse._read_stream_writers.keys())
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        keys_after = frozenset(sse._read_stream_writers.keys())
        new_keys = keys_after - keys_before
        if len(new_keys) == 1:
            sid_hex = next(iter(new_keys)).hex
            _RECENT_MCP_SESSION_HEX.append(sid_hex)
            _mcp_debug(
                "SSE sesión registrada en deque",
                session_masked=_mcp_mask_token(sid_hex),
                deque_len=len(_RECENT_MCP_SESSION_HEX),
                writers_total=len(keys_after),
            )
        await mcp_server.run(
            streams[0],
            streams[1],
            mcp_server.create_initialization_options(),
        )
    return AlreadyHandledResponse()


@app.post("/mcp")
async def handle_messages(request: Request) -> Response:
    """
    POST /mcp: Streamable HTTP (Agent Platform, ADK) o SSE legacy si hay session_id en query.
    """
    if _is_streamable_http_request(request):
        return await _handle_streamable_http(request)

    initial_sid = request.query_params.get("session_id")
    writers = getattr(sse, "_read_stream_writers", None)
    nwriters = len(writers) if isinstance(writers, dict) else 0
    if not initial_sid and nwriters == 0:
        logger.warning(
            "POST /mcp SSE sin session_id y sin sesiones en esta instancia (writers=0). "
            "Si el cliente es SSE: GET y POST deben ir a la misma réplica (affinity o "
            "max-instances=1). Si es Agent Platform: debe usar Streamable HTTP (POST sin "
            "session_id en query); redeploy con soporte Streamable HTTP en este servicio."
        )
    return await _mcp_post_message(request)


@app.delete("/mcp")
async def mcp_delete(request: Request) -> Response:
    """Cierre de sesión Streamable HTTP (protocolo MCP)."""
    if _is_streamable_http_request(request):
        return await _handle_streamable_http(request)
    return Response(status_code=405, headers={"Allow": "GET, POST, DELETE"})


@app.post("/messages")
async def mcp_messages(request: Request) -> Response:
    """Igual que POST /mcp; conservado por compatibilidad."""
    if _is_streamable_http_request(request):
        return await _handle_streamable_http(request)
    return await _mcp_post_message(request)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness/readiness ligero para probes HTTP (el TCP probe de Cloud Run usa el puerto)."""
    return {"status": "ok"}


@app.post("/google-chat")
async def google_chat_webhook(request: Request) -> dict[str, str]:
    """
    Webhook de Google Chat: reenvía mensajes de usuario al Reasoning Engine remoto.

    Respuesta esperada por Chat: ``{"text": "<respuesta>"}``.
    """
    try:
        event = await request.json()
    except Exception as exc:
        logger.warning("Google Chat: JSON inválido: %s", exc)
        return {"text": "No pude leer el mensaje (formato JSON inválido)."}

    if not isinstance(event, dict):
        return {"text": "Evento de Chat no reconocido."}

    if event.get("type") != "MESSAGE":
        return {}

    message = event.get("message")
    if not isinstance(message, dict):
        return {"text": "El evento no incluye un mensaje válido."}

    user_message = (message.get("text") or "").strip()
    if not user_message:
        return {"text": "No recibí texto en el mensaje."}

    if _gemini_reasoning_engine is None:
        if GOOGLE_CLOUD_PROJECT and GEMINI_AGENT_LOCATION and GEMINI_AGENT_ID:
            await asyncio.to_thread(_init_gemini_reasoning_engine)

    if _gemini_reasoning_engine is None:
        return {
            "text": (
                "El agente de Gemini no está disponible. "
                "Revise GOOGLE_CLOUD_PROJECT, GEMINI_AGENT_LOCATION, GEMINI_AGENT_ID "
                "y los permisos IAM de la cuenta de servicio de Cloud Run."
            )
        }

    try:
        agent_response = await asyncio.to_thread(
            _gemini_reasoning_engine.query,
            input=user_message,
        )
        text_reply = _extract_reasoning_engine_output(agent_response)
        if not text_reply:
            text_reply = "El agente no devolvió contenido en la respuesta."
        return {"text": text_reply}
    except Exception as exc:
        logger.exception("Google Chat: error al invocar Reasoning Engine")
        return {"text": f"Error al consultar al agente: {exc!s}"}
