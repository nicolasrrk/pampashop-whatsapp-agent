# agent/providers/base.py — Clase base para proveedores de WhatsApp
# Generado por AgentKit

"""
Define la interfaz comun que todos los proveedores de WhatsApp implementan.
Gracias a esto, main.py no sabe ni le importa con cual estas conectado.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from fastapi import Request

logger = logging.getLogger("agentkit")


@dataclass
class MensajeEntrante:
    """Mensaje normalizado: el mismo formato sin importar el proveedor."""

    telefono: str            # Numero del remitente, solo digitos, sin "+"
    texto: str               # Contenido del mensaje
    mensaje_id: str          # Id del mensaje en la plataforma
    es_propio: bool          # True si lo mando el agente (se ignora)
    contexto: dict = field(default_factory=dict)
    # "contexto" lleva lo que cada proveedor necesita para poder responder:
    #   evento_id       -> id unico del evento, para no procesar dos veces lo mismo
    #   conversation_id -> Zernio: en que conversacion hay que responder
    #   account_id      -> Zernio: que cuenta de WhatsApp recibio el mensaje


class ProveedorWhatsApp(ABC):
    """Interfaz que cada proveedor de WhatsApp debe implementar."""

    @abstractmethod
    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Extrae y normaliza los mensajes del payload del webhook."""
        ...

    @abstractmethod
    async def enviar_mensaje(
        self, telefono: str, mensaje: str, contexto: dict | None = None
    ) -> bool:
        """Envia un mensaje de texto. Retorna True si salio bien."""
        ...

    async def enviar_plantilla(
        self, telefono: str, nombre: str, idioma: str, parametros: list[str]
    ) -> bool:
        """
        Envia un mensaje de plantilla (template) aprobada por Meta.

        A diferencia de enviar_mensaje() (texto libre), una plantilla SI puede abrir
        una conversacion sin que el destinatario le haya escrito antes al bot en las
        ultimas 24 horas. Hace falta para avisos que el bot inicia por su cuenta, como
        la escalacion a un humano: el celular del local nunca le escribe al numero del
        bot, asi que la ventana de 24hs nunca se abre y el texto libre se pierde en
        silencio (Meta responde 200 igual, pero no entrega nada).

        Por defecto no soportado: cada proveedor que lo implemente lo sobreescribe.
        """
        logger.warning(
            f"{self.__class__.__name__} no implementa el envio de plantillas: "
            f"no se pudo mandar '{nombre}' a {telefono}"
        )
        return False

    async def verificar_firma(self, request: Request) -> bool:
        """
        Confirma que el webhook viene de verdad del proveedor.
        Por defecto acepta todo; cada proveedor lo implementa segun su esquema.
        """
        return True

    async def validar_webhook(self, request: Request) -> str | None:
        """Verificacion GET del webhook. Solo Meta la usa. Retorna la respuesta o None."""
        return None

    async def verificar_conexion(self) -> tuple[bool, str]:
        """Chequea que las credenciales sirvan. Retorna (ok, mensaje_legible)."""
        return True, "Este proveedor no expone un chequeo de conexion"
