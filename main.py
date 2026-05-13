import logging
import os
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
# Si es true, no se infiere session_id cuando solo hay una sesión SSE (modo multi-cliente).
MCP_SSE_STRICT_SESSION = os.getenv("MCP_SSE_STRICT_SESSION", "").lower() in ("1", "true", "yes")


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
                return [types.TextContent(type="text", text=str(response.json()))]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error al consultar: {str(e)}")]

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
                msg = info.get("messages", ["Sin mensaje"])[-1]
                resumen.append(f"- {recurso}: {msg}")

            texto_final = f"Resultado de la acción {accion} en {ambiente}:\n" + "\n".join(resumen)
            return [types.TextContent(type="text", text=texto_final)]

        except Exception as e:
            return [types.TextContent(type="text", text=f"Error al ejecutar acción: {str(e)}")]

    return [types.TextContent(type="text", text="Herramienta no encontrada.")]


# --- FastAPI: aplicación y transporte SSE (MCP) ---
app = FastAPI(
    title="Gestor ambientes ETRM (MCP)",
    description="Servidor MCP sobre HTTP+SSE para consulta y control de ambientes.",
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


def _scope_for_mcp_post(request: Request) -> Scope:
    """
    El transporte SSE espera `session_id` en la query. Compatibilidad:
    - Cabecera `mcp-session-id` o `x-session-id` (hex o UUID).
    - Si `MCP_SSE_STRICT_SESSION` no está activado y solo hay una sesión SSE abierta,
      se usa esa sesión (clientes que POSTean a `/mcp` sin query ni cabeceras).
    """
    scope = request.scope
    raw_qs = scope.get("query_string") or b""
    try:
        params = dict(parse_qsl(raw_qs.decode("latin-1"), keep_blank_values=True))
    except ValueError:
        params = {}

    if params.get("session_id"):
        return scope

    header_sid = (
        (request.headers.get(MCP_SESSION_ID_HEADER) or "").strip()
        or (request.headers.get("x-session-id") or "").strip()
    )
    if header_sid:
        params["session_id"] = _normalize_session_token(header_sid)
        return _scope_with_query_string(scope, params)

    if not MCP_SSE_STRICT_SESSION:
        writers = getattr(sse, "_read_stream_writers", None)
        if isinstance(writers, dict) and len(writers) == 1:
            only_id = next(iter(writers.keys()))
            params["session_id"] = only_id.hex
            logger.warning(
                "MCP POST sin session_id ni cabecera de sesión: "
                "enrutando a la única sesión SSE activa. "
                "Para varios clientes, define MCP_SSE_STRICT_SESSION=1 y corrige el cliente."
            )
            return _scope_with_query_string(scope, params)

    return scope


async def _mcp_post_message(request: Request) -> Response:
    """Delegado ASGI para JSON-RPC; la respuesta real la emite el transporte MCP."""
    scope = _scope_for_mcp_post(request)
    await sse.handle_post_message(scope, request.receive, request._send)
    return AlreadyHandledResponse()


@app.get("/mcp")
async def mcp_sse(request: Request) -> Response:
    """
    Conexión SSE del protocolo MCP: el cliente abre GET aquí y recibe el evento
    `endpoint` con la URL relativa `POST /mcp?session_id=<hex>` para mensajes JSON-RPC.
    """
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await mcp_server.run(
            streams[0],
            streams[1],
            mcp_server.create_initialization_options(),
        )
    return AlreadyHandledResponse()


@app.post("/mcp")
async def mcp_post_on_sse_path(request: Request) -> Response:
    """
    JSON-RPC MCP en el mismo path que el SSE. Orden de resolución de sesión:
    query `session_id`, cabecera `mcp-session-id` / `x-session-id`, o (si no
    `MCP_SSE_STRICT_SESSION`) la única sesión SSE abierta en este proceso.
    """
    return await _mcp_post_message(request)


@app.post("/messages")
async def mcp_messages(request: Request) -> Response:
    """Igual que POST /mcp; conservado por compatibilidad."""
    return await _mcp_post_message(request)
