# agent/escalacion.py — Deteccion y aviso de escalacion a humano
"""
Logica de "pasar a un humano". Hay dos caminos que llegan a lo mismo:

1. Por palabra clave (detectar_palabra_clave, en main.py, ANTES de llamar al modelo):
   si el mensaje trae una palabra de la lista, ni se gasta la llamada a Claude. Barato
   y rapido, pero depende de una lista fija — un cliente que pida lo mismo con otras
   palabras no la dispara.
2. Por decision del agente (escalar_desde_agente, llamada como herramienta desde
   brain.py): Fran puede reconocer que hace falta un humano en casos que la lista de
   palabras no cubre (se probo esto: un cliente escribio "quiero hablar con una
   persona" sin decir "persona real" ni "hablar con alguien" tal cual, y Fran penso
   que derivo pero el sistema nunca se entero). Este camino es el respaldo para esos
   casos.

Los dos terminan igual: marcan la conversacion como escalada (agent/memory.py se
encarga de que desde ahi el agente no le vuelva a contestar) y avisan por un canal
interno aparte — Slack o un WhatsApp interno — para que una persona siga el caso por
fuera del bot.
"""

import logging
import os

import httpx
import yaml

from agent.memory import marcar_escalado

logger = logging.getLogger("agentkit")

MENSAJE_ESCALACION_DEFAULT = (
    "Te sigue una persona de nuestro equipo a partir de ahora. En breve se pone en "
    "contacto con vos por acá."
)


def _cargar_config_escalacion() -> dict:
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}
    return (data.get("agente") or {}).get("escalacion_config") or {}


def detectar_palabra_clave(texto: str) -> str | None:
    """Devuelve la palabra de la lista que aparece en el mensaje, o None si no dispara nada."""
    palabras = _cargar_config_escalacion().get("palabras_clave") or []
    texto_normalizado = texto.lower()
    for palabra in palabras:
        if palabra.lower() in texto_normalizado:
            return palabra
    return None


def obtener_mensaje_escalacion() -> str:
    return _cargar_config_escalacion().get("mensaje") or MENSAJE_ESCALACION_DEFAULT


async def avisar_canal_interno(telefono: str, mensaje_cliente: str, motivo: str) -> None:
    """
    Manda el aviso de escalacion. Usa Slack si esta configurado; si no, un WhatsApp
    interno; si no hay ninguno, solo lo deja en el log (igual que el kit nuevo: "aviso
    sin canal configurado").
    """
    texto = f'Escalación ({motivo}) de {telefono}: "{mensaje_cliente}"'
    webhook_slack = os.getenv("ESCALACION_SLACK_WEBHOOK", "")
    numero_interno = os.getenv("ESCALACION_WHATSAPP_NUMERO", "")

    if webhook_slack:
        try:
            async with httpx.AsyncClient(timeout=10.0) as cliente:
                r = await cliente.post(webhook_slack, json={"text": texto})
            if r.status_code >= 300:
                logger.error(f"Slack rechazo el aviso de escalacion [{r.status_code}]: {r.text[:300]}")
        except httpx.HTTPError as e:
            logger.error(f"No se pudo avisar la escalacion por Slack: {e}")
        return

    if numero_interno:
        from agent.providers import obtener_proveedor

        try:
            proveedor = obtener_proveedor()
            # Texto libre, no plantilla: el numero interno (el WhatsApp del local) NUNCA
            # le escribe al numero del bot, asi que la ventana de 24hs de WhatsApp nunca
            # se abre para el. Un mensaje de texto libre en ese caso Meta lo acepta
            # (200 OK) pero no lo entrega, en silencio. Confirmado a mano: probamos
            # mandar texto libre y no llego nada; con la plantilla "escalacion_aviso" si.
            enviado = await proveedor.enviar_plantilla(
                numero_interno, "escalacion_aviso", "es_AR", [telefono, motivo, mensaje_cliente]
            )
            if not enviado:
                logger.error("El proveedor no pudo mandar el aviso de escalacion al WhatsApp interno")
        except Exception as e:  # noqa: BLE001 — un aviso que falla no debe tumbar el manejo del mensaje
            logger.error(f"No se pudo avisar la escalacion por WhatsApp interno: {e}")
        return

    logger.warning(f"Escalacion sin canal interno configurado (ESCALACION_SLACK_WEBHOOK / "
                    f"ESCALACION_WHATSAPP_NUMERO vacios): {texto}")


async def escalar_desde_agente(telefono: str, motivo: str, mensaje_cliente: str) -> str:
    """
    Escalacion pedida por el propio Fran, como herramienta, en vez de por deteccion
    de palabra clave. "telefono" viene siempre del backend (main.py / brain.py), NUNCA
    del modelo: no hay que confiar en que Claude devuelva el numero correcto del
    cliente, ademas de que ni falta hace pedirselo.

    Hace exactamente lo mismo que la escalacion por palabra clave (marcar, avisar) y
    devuelve el mismo mensaje fijo, para que la respuesta al cliente sea siempre la
    misma frase controlada y no algo que el modelo redacte en el momento — la
    prohibicion de prometer tiempos o telefonos es mas facil de sostener en un texto
    fijo que confiando en que el modelo la respete siempre.
    """
    await marcar_escalado(telefono)
    await avisar_canal_interno(telefono, mensaje_cliente, motivo or "el agente lo considero necesario")
    logger.info(f"{telefono} escalado por decision del agente: {motivo}")
    return obtener_mensaje_escalacion()
