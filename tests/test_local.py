# tests/test_local.py — Simulador de chat en terminal
# Generado por AgentKit

"""
Prueba tu agente sin necesitar WhatsApp.
Simula una conversacion en la terminal.
"""

import asyncio
import os
import sys

# La consola de Windows suele quedar en cp1252, que no puede representar varios
# caracteres que el modelo devuelve (espacios especiales, guiones largos, etc.).
# Sin esto, un print() con uno de esos caracteres tira UnicodeEncodeError y mata
# el test a mitad de la conversacion.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Agregar el directorio raiz al path para poder importar "agent"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.brain import generar_respuesta  # noqa: E402
from agent.escalacion import detectar_palabra_clave, obtener_mensaje_escalacion  # noqa: E402
from agent.memory import (  # noqa: E402
    esta_escalado,
    guardar_mensaje,
    inicializar_db,
    limpiar_historial,
    marcar_escalado,
    obtener_historial,
    reactivar_lead,
    registrar_contacto,
)

TELEFONO_TEST = "test-local-001"


async def main():
    """Loop principal del chat de prueba."""
    await inicializar_db()

    print()
    print("=" * 55)
    print("   AgentKit — Test Local — Pampa (PAMPA SHOP)")
    print("=" * 55)
    print()
    print("  Escribe mensajes como si fueras un cliente.")
    print("  Comandos especiales:")
    print("    'limpiar'    — borra el historial")
    print("    'reactivar'  — si el chat quedo escalado, vuelve a habilitar al agente")
    print("    'salir'      — termina el test")
    print()
    print("  Nota: este simulador prueba el CEREBRO (Groq + Tienda Nube) y la")
    print("  escalacion por palabra clave. El modo borrador (aprobar antes de")
    print("  enviar) solo aplica al flujo real de WhatsApp, via scripts/bandeja.py.")
    print()
    print("-" * 55)
    print()

    while True:
        try:
            mensaje = input("Tu: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nTest finalizado.")
            break

        if not mensaje:
            continue

        if mensaje.lower() == "salir":
            print("\nTest finalizado.")
            break

        if mensaje.lower() == "limpiar":
            await limpiar_historial(TELEFONO_TEST)
            print("[Historial borrado]\n")
            continue

        if mensaje.lower() == "reactivar":
            await reactivar_lead(TELEFONO_TEST)
            print("[Chat reactivado: el agente vuelve a contestar]\n")
            continue

        await registrar_contacto(TELEFONO_TEST, mensaje)

        if await esta_escalado(TELEFONO_TEST):
            print("\nPampa: [el agente no contesta: este chat esta escalado a un humano]")
            print("       (escribi 'reactivar' para volver a probar)\n")
            continue

        palabra = detectar_palabra_clave(mensaje)
        if palabra:
            mensaje_escalacion = obtener_mensaje_escalacion()
            await marcar_escalado(TELEFONO_TEST)
            await guardar_mensaje(TELEFONO_TEST, "user", mensaje)
            await guardar_mensaje(TELEFONO_TEST, "assistant", mensaje_escalacion)
            print(f"\nPampa: {mensaje_escalacion}")
            print(f"       [escalado por la palabra '{palabra}'; en producción se avisaría")
            print("       por el canal interno configurado en ESCALACION_SLACK_WEBHOOK /")
            print("       ESCALACION_WHATSAPP_NUMERO]\n")
            continue

        # El historial se lee ANTES de guardar (brain.py agrega el mensaje actual)
        historial = await obtener_historial(TELEFONO_TEST)

        print("\nPampa: ", end="", flush=True)
        respuesta, es_respuesta_real = await generar_respuesta(mensaje, historial)
        print(respuesta)
        print()

        # Igual que en produccion: los avisos tecnicos no entran al historial
        if es_respuesta_real:
            await guardar_mensaje(TELEFONO_TEST, "user", mensaje)
            await guardar_mensaje(TELEFONO_TEST, "assistant", respuesta)


if __name__ == "__main__":
    asyncio.run(main())
