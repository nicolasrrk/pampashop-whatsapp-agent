# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente.
Funciona con cualquier proveedor (Zernio, Meta) gracias a la capa de providers.
"""

import asyncio
import json
import logging
import os
from collections import defaultdict
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from agent.brain import generar_respuesta, obtener_mensaje_error, obtener_mensaje_tipo_no_soportado
from agent.escalacion import (
    AVISO_ESCALACION_COOLDOWN,
    avisar_canal_interno,
    detectar_palabra_clave,
    obtener_mensaje_escalacion,
)
from agent.memory import (
    crear_borrador,
    debe_reavisar_escalacion,
    guardar_mensaje,
    inicializar_db,
    liberar_evento,
    limpiar_eventos_viejos,
    marcar_escalado,
    marcar_evento_procesado,
    obtener_historial,
    registrar_contacto,
)
from agent.panel import PANEL_TOKEN as _PANEL_TOKEN
from agent.panel import router as panel_router
from agent.providers import obtener_proveedor
from agent.providers.base import MensajeEntrante

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

PANEL_TOKEN_CONFIGURADO = bool(_PANEL_TOKEN)

# Railway expone el commit desplegado. Sirve para saber, con un curl a "/", si el
# deploy tomo el ultimo push o quedo en una version vieja.
_sha = os.getenv("RAILWAY_GIT_COMMIT_SHA") or ""
VERSION_DESPLEGADA = _sha[:7] if _sha else "local"

# El default es "borrador", igual que whatsapp-closer-agentkit: el agente redacta,
# muestra y espera aprobacion antes de que le llegue algo al cliente. Solo pasa a
# mandar directo si se pone MODO_ENVIO=automatico a proposito.
MODO_ENVIO = (os.getenv("MODO_ENVIO") or "borrador").strip().lower()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentkit")
# En desarrollo queremos el detalle de NUESTRO agente, no el de las librerias.
# Poner el nivel raiz en DEBUG llena la terminal de ruido de aiosqlite y httpx
# y hace imposible leer lo que hizo el agente.
logger.setLevel(logging.DEBUG if ENVIRONMENT == "development" else logging.INFO)

PORT = int(os.getenv("PORT", "8000"))

# Un candado por numero de telefono. En WhatsApp es normal que alguien mande "hola" y
# medio segundo despues la pregunta de verdad: sin esto los dos mensajes se procesarian
# en paralelo, los dos leerian el mismo historial y las escrituras quedarian intercaladas.
_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Si la configuracion esta mal, guardamos el error y lo mostramos en el health check,
# en vez de reventar en el import y dejar a Railway reiniciando el contenedor a ciegas.
proveedor = None
error_configuracion: str | None = None
try:
    proveedor = obtener_proveedor()
except Exception as e:  # noqa: BLE001 — cualquier problema de configuracion
    error_configuracion = str(e)

# Resultado del chequeo de credenciales que se hace al arrancar. Se expone en el health
# check: que el servidor conteste no significa que el agente pueda responder por WhatsApp.
estado_proveedor: dict = {"ok": None, "detalle": "sin verificar"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepara la base de datos y chequea el proveedor al arrancar."""
    await inicializar_db()
    await limpiar_eventos_viejos()
    logger.info("Base de datos lista")
    logger.info(f"Servidor AgentKit escuchando en el puerto {PORT}")

    global estado_proveedor
    if proveedor is not None:
        logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Conexion con el proveedor: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor de WhatsApp NO configurado: {error_configuracion}")

    yield


app = FastAPI(title="AgentKit — WhatsApp AI Agent", version="2.0.0", lifespan=lifespan)

# Panel de solo lectura para mirar las conversaciones (agent/panel.py). Se monta
# siempre: si PANEL_TOKEN no esta configurado, sus rutas devuelven 404 solas.
app.include_router(panel_router)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway y monitoreo."""
    if error_configuracion:
        return {"status": "error", "service": "agentkit", "detalle": error_configuracion}

    # Se responde 200 aunque las credenciales esten mal, para que Railway no marque el
    # deploy como caido y puedas leer el diagnostico. El detalle esta en el cuerpo.
    return {
        "status": "ok" if estado_proveedor["ok"] else "degradado",
        "service": "agentkit",
        "proveedor": proveedor.__class__.__name__ if proveedor else None,
        "conexion": estado_proveedor,
        "modo_envio": MODO_ENVIO,
        # Estos tres son para diagnosticar a distancia. Sin ellos, un /panel que
        # devuelve 404 puede ser "falta la variable" o "el deploy quedo viejo", y
        # desde afuera se ven identicos: 404 es tambien lo que responde FastAPI para
        # una ruta que no existe.
        "panel": "activo" if PANEL_TOKEN_CONFIGURADO else "sin PANEL_TOKEN",
        "version": VERSION_DESPLEGADA,
    }


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificacion GET del webhook. La pide Meta; para Zernio no hace nada."""
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    respuesta = await proveedor.validar_webhook(request)
    if respuesta is not None:
        return PlainTextResponse(respuesta)

    # Meta pide un 403 cuando manda hub.mode=subscribe y el verify_token no coincide.
    # Devolverle 200 le hace creer que la URL quedo verificada cuando no es cierto.
    if request.query_params.get("hub.mode") == "subscribe":
        raise HTTPException(status_code=403, detail="Verify token incorrecto")

    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request, tareas: BackgroundTasks):
    """
    Recibe los mensajes de WhatsApp.

    Contesta 200 de inmediato y procesa el mensaje en segundo plano.

    Esto NO es un detalle de estilo. Los proveedores esperan un 2xx en unos 5 segundos y,
    si no lo reciben, reintentan el mismo evento hasta 7 veces. Como llamar a Claude tarda
    mas que eso, procesar antes de contestar hace que el cliente reciba la misma respuesta
    repetida. Por eso: responder primero, trabajar despues.
    """
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    if not await proveedor.verificar_firma(request):
        raise HTTPException(status_code=401, detail="Firma del webhook invalida")

    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception as e:  # noqa: BLE001
        # Un payload raro no debe hacer que el proveedor reintente para siempre
        logger.error(f"No se pudo leer el webhook: {e}")
        return {"status": "ignorado"}

    encolados = 0
    for msg in mensajes:
        if msg.es_propio or not msg.texto.strip():
            continue

        # La entrega es "al menos una vez": el mismo evento puede llegar dos veces
        evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
        if evento_id and not await marcar_evento_procesado(evento_id):
            logger.info(f"Evento repetido, se ignora: {evento_id}")
            continue

        logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")
        tareas.add_task(procesar_mensaje, msg)
        encolados += 1

    return {"status": "ok", "encolados": encolados}


async def procesar_mensaje(msg: MensajeEntrante):
    """
    Genera la respuesta y la manda de vuelta. Corre fuera del ciclo del webhook.

    Se toma un candado por telefono: dos mensajes seguidos del mismo cliente se
    atienden en orden, no en paralelo, para que el historial no se mezcle.
    """
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id

    async with _candados[msg.telefono]:
        try:
            # CRM: se registra CUALQUIER mensaje entrante, escale o no, para que
            # scripts/leads.py muestre quien escribio sin tener que abrir WhatsApp.
            await registrar_contacto(msg.telefono, msg.texto)

            # Escalar ya NO apaga al bot. Antes, apenas se marcaba una conversacion,
            # el agente dejaba de contestar por completo (o, en el intento anterior de
            # arreglar esto, contestaba solo un mensaje fijo de "ya te van a
            # contactar"). El usuario lo pidio explicito: el bot SIEMPRE tiene que
            # responder lo que se le pregunta, sin esquivar, este o no escalada la
            # conversacion — escalar es un aviso para que una persona se sume, no un
            # motivo para que Fran se calle.
            #
            # Lo unico que cambia con el estado "escalado" es que no se repite la
            # ceremonia de la PRIMERA vez (el mensaje fijo de "te sigue una persona...",
            # un aviso nuevo al local) en CADA mensaje que repite una palabra de la
            # lista: eso saturaria al local de avisos por la misma gestion. Pero si el
            # cliente insiste despues de un rato (mas del cooldown), es señal de que el
            # primer aviso se paso por alto, asi que se manda de nuevo — ver
            # debe_reavisar_escalacion.
            if await debe_reavisar_escalacion(msg.telefono, AVISO_ESCALACION_COOLDOWN):
                palabra = detectar_palabra_clave(msg.texto)
                if palabra:
                    await _escalar_a_humano(msg, evento_id, palabra)
                    return

            # Audio, video, documentos, o una imagen que no se pudo descargar: no se
            # llama al modelo, se responde directo con el aviso. Ver providers/meta.py.
            tipo_no_soportado = msg.contexto.get("tipo_no_soportado")
            if tipo_no_soportado:
                respuesta, es_respuesta_real = obtener_mensaje_tipo_no_soportado(tipo_no_soportado), True
            else:
                # El historial se lee ANTES de guardar el mensaje actual: brain.py
                # agrega el mensaje nuevo al final, y asi no queda duplicado.
                historial = await obtener_historial(msg.telefono)
                respuesta, es_respuesta_real = await generar_respuesta(
                    msg.texto, historial, telefono=msg.telefono, imagen=msg.contexto.get("imagen")
                )

            # Los avisos tecnicos (error/fallback) se mandan directo: frenarlos a
            # esperar aprobacion solo deja al cliente sin nada mas tiempo.
            if es_respuesta_real and MODO_ENVIO == "borrador":
                await crear_borrador(msg.telefono, msg.texto, respuesta, json.dumps(msg.contexto))
                logger.info(
                    f"Borrador creado para {msg.telefono}. Revisar con: python scripts/bandeja.py"
                )
                return

            enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta, msg.contexto)

            if not enviado:
                # El evento se marco como procesado ANTES de llegar hasta aca, para que dos
                # entregas simultaneas no se dupliquen. Si el envio fallo, hay que soltarlo:
                # si no, el reintento del proveedor se descartaria por duplicado y el cliente
                # se quedaria sin respuesta para siempre.
                logger.error(f"No se pudo enviar la respuesta a {msg.telefono}; se libera el evento")
                await liberar_evento(evento_id)
                return

            # Solo se guarda en el historial lo que de verdad es conversacion. Los avisos
            # tecnicos ("estoy teniendo problemas") no son un turno del agente: guardarlos
            # los deja contaminando el contexto de todos los mensajes que vengan despues.
            if es_respuesta_real:
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", respuesta)

            logger.info(f"Respuesta enviada a {msg.telefono}: {respuesta}")

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Error procesando el mensaje de {msg.telefono}: {e}")
            await liberar_evento(evento_id)
            try:
                await proveedor.enviar_mensaje(msg.telefono, obtener_mensaje_error(), msg.contexto)
            except Exception:  # noqa: BLE001
                logger.error("Tampoco se pudo avisarle al cliente del error")


async def _escalar_a_humano(msg: MensajeEntrante, evento_id: str, palabra: str):
    """
    Marca el numero como escalado, avisa por el canal interno, y manda (o deja en
    borrador) el unico mensaje de aviso al cliente. El aviso interno sale siempre,
    con o sin modo borrador: es una notificacion para el equipo, no algo que el
    cliente vea, asi que no tiene sentido frenarlo a esperar aprobacion.
    """
    mensaje_escalacion = obtener_mensaje_escalacion()
    await marcar_escalado(msg.telefono)
    await avisar_canal_interno(msg.telefono, msg.texto, f"palabra clave: {palabra}")

    if MODO_ENVIO == "borrador":
        await crear_borrador(msg.telefono, msg.texto, mensaje_escalacion, json.dumps(msg.contexto))
        logger.info(f"{msg.telefono} escalado por '{palabra}'. Aviso pendiente en scripts/bandeja.py")
        return

    enviado = await proveedor.enviar_mensaje(msg.telefono, mensaje_escalacion, msg.contexto)
    if enviado:
        await guardar_mensaje(msg.telefono, "user", msg.texto)
        await guardar_mensaje(msg.telefono, "assistant", mensaje_escalacion)
    else:
        await liberar_evento(evento_id)
    logger.info(f"{msg.telefono} escalado por palabra clave '{palabra}'")
