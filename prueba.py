import asyncio
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession

async def simular_ia():
    url = "http://0.0.0.0:8080/mcp"
    print(f"🔌 Conectando al servidor MCP en {url}...")
    
    try:
        # Aumentamos el timeout a 30 segundos para darle tiempo a tu API
        async with sse_client(url, timeout=30.0) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                print("✅ ¡Conexión exitosa!")
                
                print("\n🤖 IA Virtual: 'Consultando el estado de ETRM-QA'...")
                
                # Llamada a la herramienta
                resultado = await session.call_tool(
                    "ejecutar_accion_ambiente", 
                    arguments={
                        "ambiente": "ETRM-QA",
                        "accion": "encender"  # O "apagar"
                    }
                )
                
                print("\n📥 Resultado:")
                print("-" * 30)
                print(resultado.content[0].text)
                print("-" * 30)
    except Exception as e:
        print(f"\n❌ Error de lectura/conexión: {str(e)}")

if __name__ == "__main__":
    asyncio.run(simular_ia())