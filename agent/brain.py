# agent/brain.py — Cerebro del agente: conexion con Claude (Anthropic)
# Generado por AgentKit

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con la API de Anthropic (Claude).

PAMPA SHOP pidio que el agente consulte stock, precio y datos de producto REALES desde
Tienda Nube en vez de tener esa info fija en el prompt. Por eso este archivo implementa
el ciclo de tool use, llamando a las funciones de agent/tools.py.

Migrado de Groq (openai/gpt-oss-120b) a Claude el 2026-09-24. Motivos: el rate limit de
Groq (8.000 tokens/min en el plan gratis) alcanzaba para un solo cliente por minuto, y
el modelo se saltaba reglas explicitas del prompt (inventaba plazos de cambio, prometia
reservas) pese a tener contraejemplos. Claude ademas ve imagenes de forma nativa, asi
que resuelve de paso el problema de las fotos que el cliente manda por WhatsApp.
"""

import json
import logging
import os
import re

import anthropic
import yaml
from dotenv import load_dotenv

from agent.escalacion import escalar_desde_agente
from agent.tools import buscar_productos_tienda_nube, consultar_pedido, obtener_detalle_producto

load_dotenv()
logger = logging.getLogger("agentkit")

client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# El modelo se cambia desde .env, sin tocar el codigo.
#   claude-opus-5     el mas capaz, para casos que necesiten razonar mucho
#   claude-sonnet-5   el balanceado (default) — el elegido para este bot
#   claude-haiku-4-5  el mas barato y rapido
# El "or" y no el default de os.getenv: una variable declarada vacia en el .env
# devuelve "" y dejaria al agente sin modelo.
MODELO = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5"

# Esfuerzo de razonamiento: low | medium | high | xhigh | max. Un bot de WhatsApp que
# contesta preguntas de horarios, stock y precio no necesita pensar mucho: "low" da
# respuestas mas rapidas y mas baratas sin perder calidad para este tipo de consulta.
# Vacio (no default) para no mandar el parametro y dejar el default del modelo.
ESFUERZO = (os.getenv("ANTHROPIC_EFFORT") or "low").strip()

# WhatsApp son mensajes cortos, pero este tope NO es solo la respuesta: el razonamiento
# interno del modelo tambien cuenta contra el. Con el margen justo, una pregunta que
# exija pensar un poco deja al agente sin espacio para contestar.
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS") or "4096")

# Tope de idas y vueltas de herramientas por mensaje del cliente. Sin este limite, un
# encadenamiento raro de tool use podria quedar dando vueltas y disparar el costo y la
# latencia de un solo mensaje de WhatsApp.
MAX_PASOS_HERRAMIENTAS = int(os.getenv("ANTHROPIC_MAX_PASOS_HERRAMIENTAS") or "10")

# ── Herramientas disponibles para el modelo ─────────────────────────────────
# Formato nativo de Claude: "input_schema" en vez del "parameters" envuelto en
# "function" que usa el formato estilo OpenAI (el que usaba Groq). Los nombres y
# descripciones son los que el modelo lee para decidir CUANDO usarlas: cuanto mas clara
# la descripcion, menos se equivoca.
TOOLS = [
    {
        "name": "buscar_productos_tienda_nube",
        "description": (
            "Busca productos en el catalogo real de PAMPA SHOP por nombre, marca o "
            "palabra clave (ej: 'sandalia vizzano negra', 'zapatilla nino'). Devuelve "
            "una lista con id, nombre, marca, rango de precio y si tiene stock. "
            "Usala SIEMPRE que el cliente pregunte por un producto especifico, antes "
            "de dar cualquier dato de talle, precio o stock. "
            "Buscá UNA sola vez por producto, con el termino mas simple que lo "
            "identifique (la marca y el tipo de calzado alcanzan: 'zapatilla moleca'). "
            "NO agregues el talle ni el color a la busqueda, porque el buscador "
            "matchea por nombre y el talle no esta en el nombre: para saber si hay "
            "un talle usá obtener_detalle_producto sobre el id que ya encontraste. "
            "Si una busqueda no trae lo que esperabas, NO la repitas con variantes "
            "parecidas: contestale al cliente con lo que encontraste."
        ),
        "input_schema": {
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
    {
        "name": "obtener_detalle_producto",
        "description": (
            "Trae el detalle completo de UN producto puntual de Tienda Nube: "
            "descripcion (incluye altura de taco, altura de base y peso cuando el "
            "producto los tiene cargados) y cada variante con talle, color, precio, "
            "precio promocional y stock exacto. Usala despues de "
            "buscar_productos_tienda_nube, con el id del producto que interesa."
        ),
        "input_schema": {
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
    {
        "name": "consultar_pedido",
        "description": (
            "Busca un pedido ya realizado por su numero (el que ve el cliente, no un "
            "id interno) y devuelve su estado de pago y de envio. Usala cuando el "
            "cliente pregunte por el estado de una compra y te haya dado el numero de "
            "pedido."
        ),
        "input_schema": {
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
    {
        "name": "escalar_a_humano",
        "description": (
            "Deriva la conversacion a una persona del equipo AHORA. Usala cuando el "
            "cliente pide algo que vos no podes resolver con certeza: una reserva, "
            "bloquear stock, coordinar un retiro o un pago por fuera de la web, un "
            "reclamo, o cualquier gestion puntual — inclusive si no usa ninguna "
            "palabra especial, con que la intencion sea clara alcanza (\"quiero "
            "hablar con una persona\", \"necesito que alguien me ayude con esto\", "
            "etc.). Despues de llamar esta herramienta la conversacion queda cerrada "
            "para vos: no va a haber otro turno tuyo en este mensaje, el cliente ya "
            "recibe la respuesta de derivacion automaticamente. No la uses para "
            "preguntas que si podes responder vos con las otras herramientas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "description": (
                        "Resumen corto de por que hace falta un humano (ej: "
                        "'pide reservar un par', 'reclamo por un pedido', "
                        "'quiere coordinar un pago especial')."
                    ),
                }
            },
            "required": ["motivo"],
        },
    },
]

# Mapa nombre de herramienta -> funcion async que la implementa (todas en agent/tools.py).
# "escalar_a_humano" NO esta aca a proposito: a diferencia de estas tres (solo lectura,
# no necesitan saber quien pregunta), esa herramienta tiene que marcar al CLIENTE REAL
# como escalado, y el telefono no puede salir de lo que diga el modelo — sale del
# backend. Se maneja aparte, en el propio loop de generar_respuesta.
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


def obtener_mensaje_saturado() -> str:
    """
    Aviso para cuando Claude esta saturado (429). Se distingue del error generico a
    proposito: el cliente tiene que entender que su mensaje llego y que vale la pena
    esperar, no que algo se rompio.
    """
    config = cargar_config_prompts()
    return config.get(
        "saturado_message",
        "Perdón, estoy con muchas consultas en este momento. Dame un minutito y te respondo.",
    )


def obtener_mensaje_fallback() -> str:
    """Que decirle al cliente cuando no se entendio el mensaje."""
    return cargar_config_prompts().get(
        "fallback_message", "Disculpa, no entendi tu mensaje. Podrias reformularlo?"
    )


def obtener_mensaje_tipo_no_soportado(tipo: str) -> str:
    """
    Aviso para cuando el cliente manda algo que el agente todavia no puede leer:
    audio, video, documento, sticker, ubicacion, o una imagen que no se pudo descargar.

    Antes esos mensajes se descartaban en silencio (ver providers/meta.py) y el
    cliente se quedaba sin ninguna respuesta, ni siquiera un error: quedaba en
    "visto" sin saber que paso. Este mensaje reemplaza ese silencio.
    """
    nombres = {
        "image": "esa foto",
        "video": "videos",
        "audio": "audios",
        "document": "documentos",
        "sticker": "stickers",
        "location": "ubicaciones",
    }
    plantilla = cargar_config_prompts().get(
        "adjunto_no_soportado_message",
        "Perdón, no pude leer {tipo}. ¿Me contás con palabras qué necesitás? Así te ayudo igual.",
    )
    return plantilla.format(tipo=nombres.get(tipo, "ese tipo de archivo"))


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
    # Vinetas al principio de linea ("- Borcego $67.660", "* Borcego", "+ Borcego") ->
    # se borran. WhatsApp no las renderiza: al cliente le llega el guion literal. Ojo con
    # el orden: esto va DESPUES de la regla de negrita, porque un "*negrita*" al principio
    # de linea no es una vineta y no hay que tocarlo. Por eso se exige el espacio.
    texto = re.sub(r"(?m)^\s*[-*+]\s+", "", texto)
    # Listas numeradas ("1. Borcego") -> igual que arriba, se deja solo el texto.
    texto = re.sub(r"(?m)^\s*\d+[.)]\s+", "", texto)
    return texto.strip()


def _extraer_texto(respuesta) -> str:
    """
    Junta el texto de la respuesta de Claude.

    Ojo: NO se puede hacer respuesta.content[0].text. La respuesta es una lista de
    bloques y el primero no siempre es texto (puede haber bloques de tool_use o de
    pensamiento antes). Hay que filtrar por tipo.
    """
    partes = [bloque.text for bloque in respuesta.content if bloque.type == "text"]
    return "\n".join(p for p in partes if p).strip()


async def _ejecutar_herramienta(nombre: str, entrada: dict, cache: dict | None = None) -> str:
    """
    Ejecuta una herramienta pedida por el modelo y devuelve el resultado como texto.

    "cache" vive lo que dura UN mensaje del cliente y guarda lo que ya se consulto.
    El modelo tiende a repetir la misma busqueda con variantes ("moleca", "zapatilla
    moleca", "zapatilla Moleca 38"): cada repeticion es un turno mas contra la API, que
    suma latencia y costo. Si la llamada es identica a una anterior se devuelve lo
    guardado, sin pegarle de nuevo a Tienda Nube ni gastar el turno.
    """
    clave = (nombre, json.dumps(entrada, sort_keys=True))
    if cache is not None and clave in cache:
        logger.info(f"Herramienta {nombre} repetida con {entrada}: se usa el resultado cacheado")
        return cache[clave]

    funcion = _HERRAMIENTAS.get(nombre)
    if funcion is None:
        logger.warning(f"El modelo pidio una herramienta desconocida: {nombre}")
        return f"Herramienta '{nombre}' no existe."

    try:
        resultado = await funcion(entrada)
    except Exception as e:  # noqa: BLE001 — una herramienta rota no debe tirar la conversacion
        logger.exception(f"Error ejecutando la herramienta {nombre} con {entrada}: {e}")
        # El error NO se cachea: puede ser un problema pasajero de red con Tienda Nube.
        return "Hubo un error consultando esa informacion. Segui con lo que sepas o avisale al cliente."

    if cache is not None:
        cache[clave] = resultado
    return resultado


async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    telefono: str = "",
    imagen: dict | None = None,
) -> tuple[str, bool]:
    """
    Genera una respuesta con Claude, usando herramientas de Tienda Nube si hace falta.

    Args:
        mensaje: el mensaje nuevo del cliente (puede venir vacio si solo mando una foto)
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]
        telefono: el numero del cliente. Solo lo usa la herramienta escalar_a_humano,
            para marcar la conversacion correcta como escalada — nunca sale de lo que
            diga el modelo. Vacio por default para no romper llamadas que no
            necesitan escalar (tests, por ejemplo), pero en produccion siempre viene.
        imagen: opcional, {"media_type": "image/jpeg", "data": "<base64 sin prefijo>"}.
            Claude ve imagenes de forma nativa: no hace falta describirla aparte, se
            manda junto con el texto en el mismo mensaje del usuario.

    Returns:
        (texto, es_respuesta_real)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error, saturacion o fallback) y no una respuesta del agente. main.py lo usa
        para no guardar esos avisos en el historial: si se guardaran, quedarian
        contaminando el contexto de todos los mensajes siguientes.
    """
    if not imagen and (not mensaje or len(mensaje.strip()) < 2):
        return obtener_mensaje_fallback(), False

    system_prompt = cargar_system_prompt()
    mensajes: list[dict] = [{"role": m["role"], "content": m["content"]} for m in historial]

    contenido_usuario: list[dict] = []
    if imagen:
        contenido_usuario.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": imagen["media_type"],
                    "data": imagen["data"],
                },
            }
        )
    contenido_usuario.append(
        {"type": "text", "text": mensaje or "(el cliente mando esta imagen sin ningun texto)"}
    )
    mensajes.append({"role": "user", "content": contenido_usuario})

    async def _llamar(con_herramientas: bool = True):
        extra = {"tools": TOOLS} if con_herramientas else {}
        return await client.messages.create(
            model=MODELO,
            max_tokens=MAX_TOKENS,
            # El system prompt es grande y se repite en cada llamada de este mismo
            # mensaje (una por cada paso de herramientas) y de cada mensaje siguiente
            # del mismo cliente. Cachearlo hace que esas repeticiones salgan ~90% mas
            # baratas y no cuenten contra el limite de tokens por minuto.
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=mensajes,
            output_config={"effort": ESFUERZO} if ESFUERZO else {},
            **extra,
        )

    pasos = 0
    cache_herramientas: dict = {}
    try:
        respuesta = await _llamar()

        # ── Ciclo de tool use ────────────────────────────────────────────
        # Mientras el modelo pida herramientas, las ejecutamos y le devolvemos el
        # resultado, hasta que conteste con texto o se llegue al tope de pasos.
        while respuesta.stop_reason == "tool_use" and pasos < MAX_PASOS_HERRAMIENTAS:
            pasos += 1
            mensajes.append({"role": "assistant", "content": respuesta.content})

            tool_use_blocks = [b for b in respuesta.content if b.type == "tool_use"]
            resultados = []
            for tc in tool_use_blocks:
                logger.info(f"El modelo pidio la herramienta {tc.name} con {tc.input}")

                if tc.name == "escalar_a_humano":
                    # Esta corta la conversacion ACA, no sigue el ciclo normal: no
                    # tiene sentido que el modelo siga pidiendo cosas despues de
                    # derivar. El texto que le llega al cliente es siempre el mensaje
                    # fijo (obtener_mensaje_escalacion), nunca algo que Claude redacte
                    # en el momento — ver escalar_desde_agente para el motivo.
                    motivo = tc.input.get("motivo", "")
                    texto_fijo = await escalar_desde_agente(telefono, motivo, mensaje)
                    return texto_fijo, True

                # tc.input ya llega como dict: a diferencia del formato estilo OpenAI,
                # Claude no manda los argumentos como un string JSON para parsear.
                resultado = await _ejecutar_herramienta(tc.name, tc.input, cache_herramientas)
                resultados.append({"type": "tool_result", "tool_use_id": tc.id, "content": resultado})
            mensajes.append({"role": "user", "content": resultados})

            respuesta = await _llamar()

        if respuesta.stop_reason == "tool_use":
            # Se agotaron los pasos y el modelo seguia pidiendo herramientas. Antes esto
            # caia en el fallback ("no llegue a entender bien eso") y el cliente perdia
            # todo lo que ya se habia averiguado. En vez de eso se responden los tool_use
            # pendientes con un aviso y se pide una ultima respuesta SIN herramientas:
            # ya no puede pedir mas y tiene que redactar con lo que junto hasta aca.
            logger.warning(
                f"Se llego al tope de {MAX_PASOS_HERRAMIENTAS} pasos de herramientas: "
                "se pide el cierre sin herramientas"
            )
            mensajes.append({"role": "assistant", "content": respuesta.content})
            tool_use_blocks = [b for b in respuesta.content if b.type == "tool_use"]
            resultados = [
                {
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": (
                        "No podes pedir mas herramientas. Contestale al cliente ahora "
                        "con la informacion que ya tenes. Si algo quedo sin averiguar, "
                        "decilo con naturalidad y ofrece averiguarlo, pero nunca lo inventes."
                    ),
                }
                for tc in tool_use_blocks
            ]
            mensajes.append({"role": "user", "content": resultados})
            respuesta = await _llamar(con_herramientas=False)

    except anthropic.RateLimitError as e:
        # Un 429 significa que la cuenta se paso del limite de requests o tokens por
        # minuto. El SDK ya reintenta con espera, asi que si igual llego hasta aca es
        # que sigue saturado: no tiene sentido hacer esperar mas al cliente en silencio.
        logger.error(f"Rate limit de Anthropic ({MODELO}): {e}")
        return obtener_mensaje_saturado(), False
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error llamando a Claude: {e}")
        return obtener_mensaje_error(), False

    if respuesta.stop_reason == "max_tokens":
        logger.warning(
            f"La respuesta se corto por llegar al tope de {MAX_TOKENS} tokens. "
            "Si pasa seguido, sube ANTHROPIC_MAX_TOKENS o acorta el system prompt."
        )

    texto = _limpiar_formato_whatsapp(_extraer_texto(respuesta))
    if not texto:
        logger.warning("El modelo devolvio una respuesta sin texto")
        return obtener_mensaje_fallback(), False

    uso = respuesta.usage
    logger.info(
        f"Respuesta generada con {MODELO} "
        f"({uso.input_tokens} in / {uso.output_tokens} out, "
        f"{getattr(uso, 'cache_read_input_tokens', 0)} de cache, {pasos} pasos de herramientas)"
    )
    return texto, True
