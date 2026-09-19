# agent/escalacion.py — Deteccion y aviso de escalacion a humano
"""
Logica de "pasar a un humano", siguiendo el mismo criterio que whatsapp-closer-agentkit:
si el mensaje trae una palabra de la lista de escalacion, el agente NO llama al modelo.
Manda un unico mensaje fijo, marca esa conversacion como escalada (agent/memory.py se
encarga de que desde ahi el agente no le vuelva a contestar) y avisa por un canal interno
aparte — Slack o un WhatsApp interno — para que una persona siga el caso por fuera del bot.
"""

import logging
import os

import httpx
import yaml

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
            enviado = await proveedor.enviar_mensaje(numero_interno, texto)
            if not enviado:
                logger.error("El proveedor no pudo mandar el aviso de escalacion al WhatsApp interno")
        except Exception as e:  # noqa: BLE001 — un aviso que falla no debe tumbar el manejo del mensaje
            logger.error(f"No se pudo avisar la escalacion por WhatsApp interno: {e}")
        return

    logger.warning(f"Escalacion sin canal interno configurado (ESCALACION_SLACK_WEBHOOK / "
                    f"ESCALACION_WHATSAPP_NUMERO vacios): {texto}")
