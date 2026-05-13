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
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

load_dotenv()

logger = logging.getLogger(__name__)

API_KEY = os.getenv("API_KEY_AMBIENTES")
API_BASE_URL = os.getenv("API_URL_BASE", "").rstrip("/")


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
    print(
        "[MCP-BOOT] "
        f"MCP_DEBUG_env={raw_debug!r} effective_debug={_mcp_is_debug()} "
        f"MCP_SSE_STRICT_SESSION={strict!r} "
        f"API_KEY_AMBIENTES={'set' if key_ok else 'MISSING'} "
        f"API_URL_BASE_len={len(base)} "
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


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _ensure_mcp_logger_emits_debug()
    _print_mcp_boot_once()
    logger.info(
        "[MCP-BOOT] logger=%s handlers=%s debug=%s",
        __name__,
        len(logger.handlers),
        _mcp_is_debug(),
    )
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
    description="Servidor MCP sobre HTTP+SSE para consulta y control de ambientes.",
    lifespan=_lifespan,
)

# Mismo path que GET /mcp: el evento SSE `endpoint` será `/mcp?session_id=<hex>`.
sse = SseServerTransport("/mcp")


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


@app.get("/mcp")
async def mcp_sse(request: Request) -> Response:
    """
    Conexión SSE del protocolo MCP: el cliente abre GET aquí y recibe el evento
    `endpoint` con la URL relativa `POST /mcp?session_id=<hex>` para mensajes JSON-RPC.
    """
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
    Mensajes JSON-RPC del agente hacia ``POST /mcp`` (mismo path que el SSE).

    No se puede inventar una sesión MCP válida sin el flujo SSE en esta instancia:
    el transporte exige un ``session_id`` que exista en ``_read_stream_writers``.
    Un ``JSONResponse`` alternativo rompería el protocolo y podría duplicar la
    respuesta ASGI respecto a ``handle_post_message``.

    Si ves 400 en multi-réplica (p. ej. Agent Platform + Cloud Run), usa **session
    affinity** o **max-instances=1** para que GET SSE y POST compartan proceso.
    """
    initial_sid = request.query_params.get("session_id")
    writers = getattr(sse, "_read_stream_writers", None)
    nwriters = len(writers) if isinstance(writers, dict) else 0
    if not initial_sid and nwriters == 0:
        logger.warning(
            "POST /mcp sin session_id en la petición y sin sesiones SSE en esta "
            "instancia (writers=0). Causa habitual: POST en otra réplica que el GET "
            "/mcp. Mitigación: session affinity o max-instances=1 en Cloud Run."
        )
    return await _mcp_post_message(request)


@app.post("/messages")
async def mcp_messages(request: Request) -> Response:
    """Igual que POST /mcp; conservado por compatibilidad."""
    return await _mcp_post_message(request)
