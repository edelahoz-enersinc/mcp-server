import os
import httpx
import mcp.types as types
from fastapi import FastAPI, Request
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("API_KEY_AMBIENTES")
API_BASE_URL = os.getenv("API_URL_BASE", "").rstrip('/')

mcp_server = Server("gestor-ambientes-etrm")

# --- 1. DEFINICIÓN DE HERRAMIENTAS ---
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
                "required": ["ambiente"]
            }
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
                        "description": "La acción a realizar"
                    }
                },
                "required": ["ambiente", "accion"]
            }
        )
    ]

# --- 2. LÓGICA DE EJECUCIÓN ---
@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
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

    elif name == "ejecutar_accion_ambiente":
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

# --- 3. CONFIGURACIÓN FASTAPI Y SSE ---
app = FastAPI()
sse = SseServerTransport("/messages")

@app.get("/mcp")
async def handle_sse(request: Request):
    """Punto de entrada para la conexión SSE."""
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await mcp_server.run(
            streams[0],
            streams[1],
            mcp_server.create_initialization_options()
        )

@app.post("/messages")
async def handle_messages(request: Request):
    """Punto de entrada para los mensajes POST que envía la IA."""
    await sse.handle_post_message(request.scope, request.receive, request._send)
