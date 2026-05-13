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

API_KEY = os.getenv("API_KEY_AMBIENTES")
API_BASE_URL = os.getenv("API_URL_BASE", "").rstrip("/")


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


def _scope_for_mcp_post(request: Request) -> Scope:
    """
    El transporte SSE espera `session_id` en la query. Algunos clientes envían POST
    a `/mcp` sin query pero con la cabecera `mcp-session-id` (mismo nombre que el
    transporte HTTP streamable del SDK).
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

    if params.get("session_id"):
        return scope

    header_sid = (request.headers.get(MCP_SESSION_ID_HEADER) or "").strip()
    if not header_sid:
        return scope

    sid = header_sid
    if "-" in sid:
        try:
            sid = UUID(sid).hex
        except ValueError:
            sid = header_sid

    params["session_id"] = sid
    merged = dict(scope)
    merged["query_string"] = urlencode(params).encode("latin-1")
    return merged


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
    JSON-RPC MCP en el mismo path que el SSE. Usa `?session_id=` del evento `endpoint`
    o la cabecera `mcp-session-id` (hex 32 chars o UUID con guiones).
    """
    return await _mcp_post_message(request)


@app.post("/messages")
async def mcp_messages(request: Request) -> Response:
    """Compatibilidad: clientes antiguos o proxies que siguen posteando a `/messages`."""
    return await _mcp_post_message(request)
