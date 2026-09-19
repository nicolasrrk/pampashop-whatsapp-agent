# scripts/leads.py — CRM minimo: quien escribio y en que quedo cada uno
"""
Lista los contactos mas recientes con su ultimo mensaje. Los marcados como
escalados ya no reciben respuesta del agente -- se reactivan a mano desde aca.
"""

import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.memory import inicializar_db, listar_leads, reactivar_lead  # noqa: E402


async def main():
    await inicializar_db()
    leads = await listar_leads()

    if not leads:
        print("Todavia no hay contactos registrados.")
        return

    print(f"\n{len(leads)} contacto(s), mas reciente primero\n")
    for lead in leads:
        estado = "ESCALADO (el agente no le contesta)" if lead.escalado else "activo"
        print(f"{lead.telefono}  ·  {lead.veces_contactado} mensaje(s)  ·  {estado}")
        print(f"  último: {lead.ultimo_mensaje}")
        print()

    telefono = input("Telefono a reactivar (enter para salir): ").strip()
    if telefono:
        await reactivar_lead(telefono)
        print(f"{telefono} reactivado: el agente le vuelve a contestar.")


if __name__ == "__main__":
    asyncio.run(main())
