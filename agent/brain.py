# agent/brain.py — Cerebro del agente: conexion con Groq
# Generado por AgentKit

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con la API de Groq (compatible con el formato de OpenAI).

PAMPA SHOP pidio que el agente consulte stock, precio y datos de producto REALES desde
Tienda Nube en vez de tener esa info fija en el prompt. Por eso este archivo implementa
el ciclo de tool/function calling, llamando a las funciones de agent/tools.py.
"""

import json
import logging
import os
import re

import yaml
from dotenv import load_dotenv
from groq import AsyncGroq

from agent.tools import buscar_productos_tienda_nube, consultar_pedido, obtener_detalle_producto

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

# El modelo se cambia desde .env, sin tocar el codigo.
#   openai/gpt-oss-120b   el mas capaz, mejor para razonar con el catalogo real (default)
#   openai/gpt-oss-20b    mas rapido y liviano
# El "or" y no el default de os.getenv: una variable declarada vacia en el .env
# devuelve "" y dejaria al agente sin modelo.
MODELO = os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b"

# WhatsApp son mensajes cortos, pero este tope NO es solo la respuesta: el razonamiento
# interno del modelo tambien cuenta contra el. Con el margen justo, una pregunta que
# exija pensar un poco deja al agente sin espacio para contestar.
MAX_TOKENS = int(os.getenv("GROQ_MAX_TOKENS") or "4096")

# Tope de idas y vueltas de herramientas por mensaje del cliente. Sin este limite, un
# encadenamiento raro de tool use podria quedar dando vueltas y disparar el costo y la
# latencia de un solo mensaje de WhatsApp.
MAX_PASOS_HERRAMIENTAS = 5

# ── Herramientas disponibles para el modelo ─────────────────────────────────
# Formato de function calling estilo OpenAI (el que usa la API de Groq). Los nombres y
# descripciones son los que el modelo lee para decidir CUANDO usarlas: cuanto mas clara
# la descripcion, menos se equivoca.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "buscar_productos_tienda_nube",
            "description": (
                "Busca productos en el catalogo real de PAMPA SHOP por nombre, marca o "
                "palabra clave (ej: 'sandalia vizzano negra', 'zapatilla nino'). Devuelve "
                "una lista con id, nombre, marca, rango de precio y si tiene stock. "
                "Usala SIEMPRE que el cliente pregunte por un producto especifico, antes "
                "de dar cualquier dato de talle, precio o stock."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {
                        "type": "string",
                        "description": "Texto de busqueda: nombre del producto, marca y/o tipo de calzado.",
                    }
                },
                "required": ["consulta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obtener_detalle_producto",
            "description": (
                "Trae el detalle completo de UN producto puntual de Tienda Nube: "
                "descripcion (incluye altura de taco, altura de base y peso cuando el "
                "producto los tiene cargados) y cada variante con talle, color, precio, "
                "precio promocional y stock exacto. Usala despues de "
                "buscar_productos_tienda_nube, con el id del producto que interesa."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "Id numerico del producto, obtenido de buscar_productos_tienda_nube.",
                    }
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_pedido",
            "description": (
                "Busca un pedido ya realizado por su numero (el que ve el cliente, no un "
                "id interno) y devuelve su estado de pago y de envio. Usala cuando el "
                "cliente pregunte por el estado de una compra y te haya dado el numero de "
                "pedido."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "numero_pedido": {
                        "type": "string",
                        "description": "Numero de pedido que el cliente puede ver en su comprobante o email de confirmacion.",
                    }
                },
                "required": ["numero_pedido"],
            },
        },
    },
]

# Mapa nombre de herramienta -> funcion async que la implementa (todas en agent/tools.py)
_HERRAMIENTAS = {
    "buscar_productos_tienda_nube": lambda i: buscar_productos_tienda_nube(i["consulta"]),
    "obtener_detalle_producto": lambda i: obtener_detalle_producto(i["product_id"]),
    "consultar_pedido": lambda i: consultar_pedido(i["numero_pedido"]),
}


def cargar_config_prompts() -> dict:
    """Lee toda la configuracion desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """El system prompt: quien es el agente y que sabe del negocio."""
    return cargar_config_prompts().get(
        "system_prompt", "Eres un asistente util. Responde siempre en espanol."
    )


def obtener_mensaje_error() -> str:
    """Que decirle al cliente cuando algo falla de nuestro lado."""
    return cargar_config_prompts().get(
        "error_message",
        "Lo siento, estoy teniendo problemas tecnicos. Por favor intenta de nuevo en unos minutos.",
    )


def obtener_mensaje_fallback() -> str:
    """Que decirle al cliente cuando no se entendio el mensaje."""
    return cargar_config_prompts().get(
        "fallback_message", "Disculpa, no entendi tu mensaje. Podrias reformularlo?"
    )


def _limpiar_formato_whatsapp(texto: str) -> str:
    """
    Convierte el markdown tipo documento que el modelo insiste en usar (pese a que el
    prompt se lo pide explicitamente) al unico formato que WhatsApp interpreta de
    verdad. Es mas confiable arreglarlo aca que seguir puliendo el prompt: un cliente
    real no tiene por que ver un "**" o un "#" sueltos en el chat.
    """
    # "**negrita**" o "__negrita__" -> "*negrita*" (asterisco simple, el que WhatsApp
    # SI renderiza en negrita)
    texto = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texto)
    texto = re.sub(r"__(.+?)__", r"*\1*", texto)
    # Titulos markdown ("# Titulo", "## Titulo") -> el texto solo, sin los numerales
    texto = re.sub(r"(?m)^#{1,6}\s*", "", texto)
    return texto.strip()


async def _ejecutar_herramienta(nombre: str, entrada: dict) -> str:
    """Ejecuta una herramienta pedida por el modelo y devuelve el resultado como texto."""
    funcion = _HERRAMIENTAS.get(nombre)
    if funcion is None:
        logger.warning(f"El modelo pidio una herramienta desconocida: {nombre}")
        return f"Herramienta '{nombre}' no existe."

    try:
        return await funcion(entrada)
    except Exception as e:  # noqa: BLE001 — una herramienta rota no debe tirar la conversacion
        logger.exception(f"Error ejecutando la herramienta {nombre} con {entrada}: {e}")
        return "Hubo un error consultando esa informacion. Segui con lo que sepas o avisale al cliente."


async def generar_respuesta(mensaje: str, historial: list[dict]) -> tuple[str, bool]:
    """
    Genera una respuesta con Groq, usando herramientas de Tienda Nube si hace falta.

    Args:
        mensaje: el mensaje nuevo del cliente
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]

    Returns:
        (texto, es_respuesta_real)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error o fallback) y no una respuesta del agente. main.py lo usa para no
        guardar esos avisos en el historial: si se guardaran, quedarian contaminando
        el contexto de todos los mensajes siguientes.
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), False

    system_prompt = cargar_system_prompt()
    mensajes = [{"role": "system", "content": system_prompt}]
    mensajes.extend({"role": m["role"], "content": m["content"]} for m in historial)
    mensajes.append({"role": "user", "content": mensaje})

    async def _llamar():
        return await client.chat.completions.create(
            model=MODELO,
            max_tokens=MAX_TOKENS,
            messages=mensajes,
            tools=TOOLS,
            tool_choice="auto",
            # Sin esto, con tool use activo el modelo puede devolver su razonamiento
            # interno mezclado en el mismo texto de la respuesta (tags <think>), y el
            # cliente terminaria leyendo eso por WhatsApp. "parsed" lo separa aparte.
            reasoning_format="parsed",
        )

    pasos = 0
    try:
        respuesta = await _llamar()

        # ── Ciclo de tool/function calling ──────────────────────────────
        # Mientras el modelo pida herramientas, las ejecutamos y le devolvemos el
        # resultado, hasta que conteste con texto o se llegue al tope de pasos.
        while respuesta.choices[0].finish_reason == "tool_calls" and pasos < MAX_PASOS_HERRAMIENTAS:
            pasos += 1
            mensaje_modelo = respuesta.choices[0].message
            mensajes.append(
                {
                    "role": "assistant",
                    "content": mensaje_modelo.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in (mensaje_modelo.tool_calls or [])
                    ],
                }
            )

            for tc in mensaje_modelo.tool_calls or []:
                try:
                    entrada = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    entrada = {}
                logger.info(f"El modelo pidio la herramienta {tc.function.name} con {entrada}")
                resultado = await _ejecutar_herramienta(tc.function.name, entrada)
                mensajes.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": resultado}
                )

            respuesta = await _llamar()

        if respuesta.choices[0].finish_reason == "tool_calls":
            logger.warning(
                f"Se llego al tope de {MAX_PASOS_HERRAMIENTAS} pasos de herramientas sin una respuesta final"
            )

    except Exception as e:  # noqa: BLE001
        logger.error(f"Error llamando a Groq: {e}")
        return obtener_mensaje_error(), False

    eleccion = respuesta.choices[0]
    if eleccion.finish_reason == "length":
        logger.warning(
            f"La respuesta se corto por llegar al tope de {MAX_TOKENS} tokens. "
            "Si pasa seguido, sube GROQ_MAX_TOKENS o acorta el system prompt."
        )

    texto = _limpiar_formato_whatsapp(eleccion.message.content or "")
    if not texto:
        logger.warning("El modelo devolvio una respuesta sin texto")
        return obtener_mensaje_fallback(), False

    uso = respuesta.usage
    logger.info(
        f"Respuesta generada con {MODELO} "
        f"({uso.prompt_tokens} in / {uso.completion_tokens} out, {pasos} pasos de herramientas)"
    )
    return texto, True
