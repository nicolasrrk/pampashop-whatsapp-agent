# scripts/bandeja.py — Bandeja de borradores
"""
Con MODO_ENVIO=borrador (el default), el agente redacta las respuestas y las deja
acá esperando. Nada le llega al cliente hasta que las revisas con este script.
"""

import asyncio
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.memory import (  # noqa: E402
    guardar_mensaje,
    inicializar_db,
    listar_borradores_pendientes,
    marcar_borrador,
)
from agent.providers import obtener_proveedor  # noqa: E402


async def main():
    await inicializar_db()
    proveedor = obtener_proveedor()

    borradores = await listar_borradores_pendientes()
    if not borradores:
        print("No hay borradores pendientes.")
        return

    print(f"\n{len(borradores)} borrador(es) pendiente(s)\n")

    for b in borradores:
        print("=" * 60)
        print(f"De: {b.telefono}")
        print(f"Cliente dijo: {b.mensaje_cliente}")
        print(f"\nBorrador de respuesta:\n{b.respuesta}\n")

        accion = input("[A]probar y enviar / [E]ditar / [D]escartar / [S]altear (enter): ").strip().lower()

        if accion in ("", "s"):
            continue

        if accion == "d":
            await marcar_borrador(b.id, "descartado")
            print("Descartado.\n")
            continue

        texto_final = b.respuesta
        if accion == "e":
            print("Escribi la respuesta nueva (enter para dejar la de arriba):")
            nueva = input("> ").strip()
            if nueva:
                texto_final = nueva

        contexto = json.loads(b.contexto_json or "{}")
        enviado = await proveedor.enviar_mensaje(b.telefono, texto_final, contexto)

        if enviado:
            await marcar_borrador(b.id, "aprobado")
            await guardar_mensaje(b.telefono, "user", b.mensaje_cliente)
            await guardar_mensaje(b.telefono, "assistant", texto_final)
            print("Enviado.\n")
        else:
            print("No se pudo enviar (revisa los logs del servidor). Queda pendiente.\n")


if __name__ == "__main__":
    asyncio.run(main())
