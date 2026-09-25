# agent/transcripcion.py — Transcripcion de audios de WhatsApp
"""
Transcribe audios (notas de voz) a texto usando Whisper, hosteado por Groq.

Por que Groq y no Claude: la API de mensajes de Claude no tiene un tipo de contenido
para audio (solo imagen, documento y texto) — no hay forma de mandarle un audio
directo. Groq hostea Whisper con un endpoint compatible con el de OpenAI, rapido y con
un tier gratis (2.000 minutos por dia) que sobra de sobra para el volumen de WhatsApp
de un negocio. Es una integracion aparte de la que usaba brain.py para el chat antes
de migrar a Claude: el GROQ_API_KEY que quedo del proveedor anterior se reutiliza
solo para esto, no para generar respuestas.
"""

import logging
import os

import httpx

logger = logging.getLogger("agentkit")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# whisper-large-v3-turbo: la version rapida, de sobra para notas de voz cortas de
# WhatsApp. whisper-large-v3 (sin "turbo") es mas preciso pero mas lento, para si
# hiciera falta mas adelante.
MODELO_TRANSCRIPCION = os.getenv("GROQ_WHISPER_MODEL") or "whisper-large-v3-turbo"


async def transcribir_audio(datos: bytes, nombre_archivo: str = "audio.ogg") -> str | None:
    """
    Manda el audio a Whisper (Groq) y devuelve el texto transcripto en espanol, o
    None si algo fallo: sin GROQ_API_KEY, error de red, o Groq rechazo el archivo.
    """
    if not GROQ_API_KEY:
        logger.warning("Falta GROQ_API_KEY: no se puede transcribir el audio")
        return None

    try:
        async with httpx.AsyncClient(timeout=60.0) as cliente:
            r = await cliente.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (nombre_archivo, datos)},
                data={
                    "model": MODELO_TRANSCRIPCION,
                    # Se fija el idioma en vez de dejar que Whisper lo adivine: mejora
                    # la precision y evita que confunda espanol con portugues o
                    # italiano en audios cortos o con ruido de fondo del local.
                    "language": "es",
                    "response_format": "text",
                },
            )
    except httpx.HTTPError as e:
        logger.error(f"Error de red transcribiendo audio: {e}")
        return None

    if r.status_code != 200:
        logger.error(f"Groq rechazo la transcripcion [{r.status_code}]: {r.text[:300]}")
        return None

    return r.text.strip() or None
