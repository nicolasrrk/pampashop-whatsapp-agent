# agent/providers/meta.py — Adaptador para Meta WhatsApp Cloud API
# Generado por AgentKit

"""
Conexion directa contra la API oficial de Meta.
Documentacion: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

import base64
import hashlib
import hmac
import logging
import os

import httpx
from fastapi import Request

from agent.providers.base import MensajeEntrante, ProveedorWhatsApp

logger = logging.getLogger("agentkit")

# Tipos de mensaje que el agente todavia no puede leer (aparte de imagen, que ahora se
# descarga y se le manda a Claude — ver _descargar_imagen). Antes se descartaban con un
# simple "continue" en parsear_webhook: el cliente mandaba un audio y no le llegaba
# absolutamente nada, ni un error, se quedaba en visto sin saber que paso. Ahora se
# marcan con "tipo_no_soportado" para que main.py le mande un aviso claro.
_TIPOS_SIN_SOPORTE = {
    "video": "videos",
    "audio": "audios",
    "document": "documentos",
    "sticker": "stickers",
    "location": "ubicaciones",
}


def _numero_para_enviar(telefono: str) -> str:
    """
    Los celulares de Argentina llegan en los webhooks con un "9" extra despues del
    codigo de pais (ej: 5493624548139, el formato historico "9 + 10 digitos"), pero
    el endpoint de ENVIO de la Cloud API los rechaza con (#131030) "Recipient phone
    number not in allowed list" si se les manda ese mismo numero de vuelta: hay que
    sacarle el "9" antes de responder. Confirmado a mano contra la API: mandar a
    "543624548139" funciona, mandar a "5493624548139" no, para el mismo destinatario.
    """
    if telefono.startswith("549") and len(telefono) == 13:
        return "54" + telefono[3:]
    return telefono


class ProveedorMeta(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando la API oficial de Meta (Cloud API)."""

    def __init__(self):
        self.access_token = os.getenv("META_ACCESS_TOKEN", "")
        self.phone_number_id = os.getenv("META_PHONE_NUMBER_ID", "")
        # Mismo cuidado que en zernio.py: una variable declarada pero vacia en el .env
        # devuelve "" y no el default, asi que se usa "or".
        self.verify_token = os.getenv("META_VERIFY_TOKEN") or "agentkit-verify"
        self.app_secret = os.getenv("META_APP_SECRET", "")
        self.api_version = os.getenv("META_API_VERSION") or "v25.0"

        if not self.access_token or not self.phone_number_id:
            logger.warning(
                "Faltan META_ACCESS_TOKEN o META_PHONE_NUMBER_ID: el agente no va a poder responder"
            )
        if not self.app_secret:
            logger.warning(
                "META_APP_SECRET no esta configurado: los webhooks NO se verifican. "
                "Sirve para probar, pero no lo dejes asi en produccion."
            )

    # ── Recibir ──────────────────────────────────────────────────────────

    async def validar_webhook(self, request: Request) -> str | None:
        """
        Meta hace un GET con hub.challenge la primera vez, para comprobar que la URL es tuya.
        Hay que devolver el challenge tal cual, como texto plano.
        """
        params = request.query_params
        if (
            params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == self.verify_token
        ):
            return params.get("hub.challenge") or ""
        return None

    async def verificar_firma(self, request: Request) -> bool:
        """Compara el header X-Hub-Signature-256 contra el HMAC-SHA256 del cuerpo crudo."""
        if not self.app_secret:
            return True  # modo pruebas, ya se advirtio al arrancar

        cabecera = request.headers.get("X-Hub-Signature-256", "")
        if not cabecera.startswith("sha256="):
            logger.warning("Llego un webhook sin firma X-Hub-Signature-256: rechazado")
            return False

        cuerpo = await request.body()
        firma_esperada = hmac.new(
            self.app_secret.encode("utf-8"), cuerpo, hashlib.sha256
        ).hexdigest()

        # Igual que en zernio.py: compare_digest sobre str exige ASCII puro y un header
        # con bytes raros tiraria TypeError, devolviendo 500 en vez de 401.
        try:
            iguales = hmac.compare_digest(firma_esperada, cabecera.removeprefix("sha256="))
        except TypeError:
            logger.warning("La firma del webhook trae caracteres invalidos: rechazado")
            return False

        if not iguales:
            logger.warning("Firma de webhook invalida: rechazado")
            return False
        return True

    async def _descargar_imagen(self, media_id: str) -> dict | None:
        """
        Baja una imagen que el cliente mando por WhatsApp y la deja lista para Claude.

        Meta no entrega la imagen en el webhook, solo un media_id: hay que pedirle la
        URL temporal de descarga y despues bajar el archivo, los dos pasos con el
        mismo token de acceso. Devuelve None si algo falla (imagen vieja, tipo raro,
        error de red) para que el llamador pueda avisarle al cliente en vez de
        colgarse.
        """
        if not self.access_token:
            return None

        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            async with httpx.AsyncClient(timeout=20.0) as cliente:
                r = await cliente.get(
                    f"https://graph.facebook.com/{self.api_version}/{media_id}",
                    headers=headers,
                )
                if r.status_code != 200:
                    logger.error(f"No se pudo obtener la URL de la imagen [{r.status_code}]: {r.text[:300]}")
                    return None
                datos = r.json()
                url = datos.get("url")
                mime_type = datos.get("mime_type", "image/jpeg")
                if not url:
                    return None

                r2 = await cliente.get(url, headers=headers)
                if r2.status_code != 200:
                    logger.error(f"No se pudo descargar la imagen [{r2.status_code}]")
                    return None
        except httpx.HTTPError as e:
            logger.error(f"Error de red descargando una imagen de WhatsApp: {e}")
            return None

        return {"media_type": mime_type, "data": base64.standard_b64encode(r2.content).decode("ascii")}

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Recorre el payload anidado de Meta Cloud API."""
        body = await request.json()
        mensajes: list[MensajeEntrante] = []

        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                for msg in value.get("messages", []):
                    tipo = msg.get("type")

                    if tipo == "text":
                        mensajes.append(
                            MensajeEntrante(
                                telefono=msg.get("from", ""),
                                texto=(msg.get("text") or {}).get("body", ""),
                                mensaje_id=msg.get("id", ""),
                                # Meta solo entrega mensajes entrantes por este canal
                                es_propio=False,
                                contexto={"evento_id": msg.get("id", "")},
                            )
                        )

                    elif tipo == "image":
                        # Se descarga aca, en el parseo del webhook, y no mas adelante
                        # en brain.py: asi el resto del sistema no necesita saber nada
                        # de la API de medios de Meta, solo recibe la imagen ya lista.
                        bloque_imagen = msg.get("image") or {}
                        imagen = await self._descargar_imagen(bloque_imagen.get("id", ""))
                        contexto = {"evento_id": msg.get("id", "")}
                        if imagen:
                            contexto["imagen"] = imagen
                        else:
                            # No se pudo bajar la imagen: se marca como no soportada en
                            # vez de perder el mensaje, para que el cliente reciba al
                            # menos un aviso y no quede en visto sin explicacion.
                            contexto["tipo_no_soportado"] = "image"
                        mensajes.append(
                            MensajeEntrante(
                                telefono=msg.get("from", ""),
                                texto=bloque_imagen.get("caption") or "[el cliente envio una foto]",
                                mensaje_id=msg.get("id", ""),
                                es_propio=False,
                                contexto=contexto,
                            )
                        )

                    elif tipo in _TIPOS_SIN_SOPORTE:
                        mensajes.append(
                            MensajeEntrante(
                                telefono=msg.get("from", ""),
                                # El texto no puede quedar vacio: main.py descarta en
                                # silencio los mensajes sin texto (webhook_handler).
                                texto=f"[el cliente envio {_TIPOS_SIN_SOPORTE[tipo]}]",
                                mensaje_id=msg.get("id", ""),
                                es_propio=False,
                                contexto={
                                    "evento_id": msg.get("id", ""),
                                    "tipo_no_soportado": tipo,
                                },
                            )
                        )
                    # otros tipos (reacciones, contactos, interactivos, de sistema)
                    # se siguen ignorando: no son mensajes que un cliente espere
                    # que se le responda.
        return mensajes

    # ── Enviar ───────────────────────────────────────────────────────────

    async def enviar_mensaje(
        self, telefono: str, mensaje: str, contexto: dict | None = None
    ) -> bool:
        """Envia un mensaje de texto por la Cloud API. Meta no necesita el contexto."""
        if not self.access_token or not self.phone_number_id:
            logger.error("No se puede enviar: faltan META_ACCESS_TOKEN o META_PHONE_NUMBER_ID")
            return False

        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"

        try:
            async with httpx.AsyncClient(timeout=30.0) as cliente:
                r = await cliente.post(
                    url,
                    json={
                        "messaging_product": "whatsapp",
                        "to": _numero_para_enviar(telefono),
                        "type": "text",
                        "text": {"body": mensaje},
                    },
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as e:
            logger.error(f"Error de red hablando con Meta: {e}")
            return False

        if r.status_code == 200:
            return True

        logger.error(f"Meta rechazo el envio [{r.status_code}]: {r.text[:500]}")
        return False

    async def enviar_plantilla(
        self, telefono: str, nombre: str, idioma: str, parametros: list[str]
    ) -> bool:
        """
        Envia una plantilla (template) aprobada por Meta. A diferencia de un mensaje
        de texto libre, esto SI abre conversacion aunque el destinatario nunca le haya
        escrito al bot: por eso lo usa la escalacion, para el aviso al numero interno.
        """
        if not self.access_token or not self.phone_number_id:
            logger.error("No se puede enviar: faltan META_ACCESS_TOKEN o META_PHONE_NUMBER_ID")
            return False

        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        cuerpo = {
            "messaging_product": "whatsapp",
            "to": _numero_para_enviar(telefono),
            "type": "template",
            "template": {
                "name": nombre,
                "language": {"code": idioma},
                "components": (
                    [{"type": "body", "parameters": [{"type": "text", "text": p} for p in parametros]}]
                    if parametros
                    else []
                ),
            },
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as cliente:
                r = await cliente.post(
                    url,
                    json=cuerpo,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as e:
            logger.error(f"Error de red hablando con Meta: {e}")
            return False

        if r.status_code == 200:
            return True

        # El motivo mas comun de error aca es que la plantilla todavia esta en
        # revision (PENDING) o fue rechazada: Meta lo dice en el mensaje.
        logger.error(f"Meta rechazo el envio de la plantilla '{nombre}' [{r.status_code}]: {r.text[:500]}")
        return False

    # ── Diagnostico ──────────────────────────────────────────────────────

    async def verificar_conexion(self) -> tuple[bool, str]:
        """Lee el numero desde la Graph API para confirmar que el token sirve."""
        if not self.access_token or not self.phone_number_id:
            return False, "Faltan META_ACCESS_TOKEN o META_PHONE_NUMBER_ID"

        try:
            async with httpx.AsyncClient(timeout=15.0) as cliente:
                r = await cliente.get(
                    f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}",
                    params={"fields": "display_phone_number,verified_name,quality_rating"},
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )
        except httpx.HTTPError as e:
            return False, f"No se pudo contactar a Meta: {e}"

        if r.status_code != 200:
            return False, f"Meta respondio {r.status_code}: {r.text[:200]}"

        datos = r.json()
        return True, (
            f"Numero {datos.get('display_phone_number', '?')} conectado "
            f"(calidad: {datos.get('quality_rating', '?')})"
        )
