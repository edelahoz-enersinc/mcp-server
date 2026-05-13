#!/usr/bin/env python3
"""
Cliente MCP de prueba (transporte SSE). Úsalo contra el servidor local o Docker:

  ./venv/bin/python prueba.py
  ./venv/bin/python prueba.py --consultar
  MCP_SERVER_URL=http://127.0.0.1:8080/mcp ./venv/bin/python prueba.py

El servidor debe estar en marcha (uvicorn o contenedor en el mismo host/puerto).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client


def _build_parser() -> argparse.ArgumentParser:
    default_url = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8080/mcp")
    p = argparse.ArgumentParser(description="Prueba MCP vía SSE (GET /mcp + POST mensajes).")
    p.add_argument(
        "--url",
        default=default_url,
        help="URL del endpoint SSE (GET). También: variable MCP_SERVER_URL.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("MCP_CLIENT_TIMEOUT", "60")),
        help="Timeout HTTP del cliente MCP (segundos).",
    )
    p.add_argument("--ambiente", default=os.getenv("MCP_TEST_AMBIENTE", "ETRM-QA"))
    p.add_argument(
        "--accion",
        choices=("encender", "apagar"),
        default=os.getenv("MCP_TEST_ACCION", "encender"),
        help="Solo aplica si no usas --consultar.",
    )
    p.add_argument(
        "--consultar",
        action="store_true",
        help="Llama consultar_estado_ambiente; por defecto se llama ejecutar_accion_ambiente.",
    )
    return p


async def _run(args: argparse.Namespace) -> int:
    url = args.url.rstrip("/")
    if "/mcp" not in url.split("?", 1)[0]:
        print(
            f"Aviso: la URL del SSE suele incluir /mcp al final. Recibido: {url!r}",
            file=sys.stderr,
        )

    print(f"Conectando (SSE) a {url!r} …")
    try:
        async with sse_client(url, timeout=args.timeout) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                print("Sesión MCP inicializada.")

                if args.consultar:
                    print(f"Llamada: consultar_estado_ambiente(ambiente={args.ambiente!r})")
                    resultado = await session.call_tool(
                        "consultar_estado_ambiente",
                        arguments={"ambiente": args.ambiente},
                    )
                else:
                    print(
                        "Llamada: ejecutar_accion_ambiente("
                        f"ambiente={args.ambiente!r}, accion={args.accion!r})"
                    )
                    resultado = await session.call_tool(
                        "ejecutar_accion_ambiente",
                        arguments={"ambiente": args.ambiente, "accion": args.accion},
                    )

                print("\nResultado:")
                print("-" * 40)
                for block in resultado.content:
                    if hasattr(block, "text"):
                        print(block.text)
                print("-" * 40)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    load_dotenv()
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
