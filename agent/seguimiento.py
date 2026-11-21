# agent/seguimiento.py — Seguimiento de posibles ventas que no se concretaron

"""
Un cliente pregunto por productos, el bot le paso links, y no compro. Entre 18 y 23,5
horas despues de su ultimo mensaje (todavia DENTRO de la ventana de 24 h de WhatsApp,
asi que va como texto libre: sin plantilla y sin costo de Meta), el bot le escribe UNA
vez: "¿pudiste avanzar con la compra?".

Filtros, en orden de costo (lo barato primero):
  1. Interruptor del panel prendido (apagado por defecto).
  2. Horario: solo de 9 a 21 h de Argentina.
  3. Chat con bot activo, no escalado, sin mensajes del equipo en el ultimo dia.
  4. El bot le paso al menos un link de producto en ese tramo de la charla.
  5. No hay un pedido en Tienda Nube con ese celular desde que empezo la charla.
  6. Claude lee la charla y decide si corresponde (no si ya compro en el local, si dijo
     que no le interesa, si era un reclamo...) y redacta el mensaje.
Cada chat se evalua una sola vez por "tramo" (ultimo mensaje del cliente), y nunca se le
manda mas de un seguimiento por semana a la misma persona.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from agent.memory import (
    ZONA_ARGENTINA,
    Lead,
    Mensaje,
    Seguimiento,
    async_session,
    crear_borrador,
    guardar_mensaje,
    obtener_config,
)

logger = logging.getLogger("agentkit")

CLAVE_ACTIVO = "seguimiento_ventas_activo"  # en la tabla configuracion: "1" prendido
ESPERA_MINIMA = timedelta(hours=18)
ESPERA_MAXIMA = timedelta(hours=23, minutes=30)  # la ventana de WhatsApp cierra a las 24 h
HORA_DESDE, HORA_HASTA = 9, 21  # hora de Argentina
SIN_REPETIR = timedelta(days=7)  # a una misma persona, como mucho un seguimiento por semana
INTERVALO = 10 * 60  # cada cuanto se revisa, en segundos
MARCA_LINK_PRODUCTO = "pampashop.com.ar/productos/"


async def esta_activo() -> bool:
    return (await obtener_config(CLAVE_ACTIVO)) == "1"


def _hora_permitida(momento: datetime) -> bool:
    return HORA_DESDE <= momento.astimezone(ZONA_ARGENTINA).hour < HORA_HASTA


def _utc(fecha: datetime) -> datetime:
    # SQLite devuelve fechas sin zona aunque se hayan guardado en UTC
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


def _digitos_finales(telefono: str | None, n: int = 10) -> str:
    """Ultimos 10 digitos: iguala 5493624123456 (WhatsApp) con +543624123456 (Tienda Nube)."""
    digitos = re.sub(r"\D", "", telefono or "")
    return digitos[-n:] if len(digitos) >= n else ""


# ── 1. Candidatos (solo base de datos, sin costo) ────────────────────────────


async def _candidatos(ahora: datetime) -> list[dict]:
    """Chats cuyo ultimo mensaje del cliente cae en la franja 18 h - 23,5 h atras."""
    desde, hasta = ahora - ESPERA_MAXIMA, ahora - ESPERA_MINIMA
    resultado = []
    async with async_session() as session:
        leads = (
            await session.execute(
                select(Lead).where(
                    Lead.bot_activo.is_(True),
                    Lead.escalado.is_(False),
                    Lead.actualizado_en >= desde - timedelta(hours=1),
                )
            )
        ).scalars().all()

        for lead in leads:
            ultimo_cliente = await session.scalar(
                select(Mensaje)
                .where(Mensaje.telefono == lead.telefono, Mensaje.role == "user")
                .order_by(Mensaje.id.desc())
                .limit(1)
            )
            if ultimo_cliente is None or not (desde <= _utc(ultimo_cliente.timestamp) <= hasta):
                continue

            # Ya se evaluo este tramo, o ya se le mando uno esta semana: no se vuelve a mirar
            ya = await session.scalar(
                select(Seguimiento.id).where(
                    Seguimiento.telefono == lead.telefono,
                    (Seguimiento.mensaje_cliente_id == ultimo_cliente.id)
                    | (
                        Seguimiento.estado.in_(("enviado", "borrador"))
                        & (Seguimiento.creado_en >= ahora - SIN_REPETIR)
                    ),
                ).limit(1)
            )
            if ya is not None:
                continue

            # El tramo de charla: el ultimo dia antes del ultimo mensaje del cliente, y todo lo posterior
            inicio_tramo = _utc(ultimo_cliente.timestamp) - timedelta(hours=24)
            charla = (
                await session.execute(
                    select(Mensaje)
                    .where(Mensaje.telefono == lead.telefono, Mensaje.timestamp >= inicio_tramo)
                    .order_by(Mensaje.id.asc())
                )
            ).scalars().all()
            resultado.append({"lead": lead, "ultimo_cliente": ultimo_cliente, "charla": list(charla)})
    return resultado


def _filtro_rapido(charla: list) -> str | None:
    """Motivo para descartar sin gastar nada, o None si sigue en carrera."""
    if any(m.remitente == "humano" for m in charla):
        return "lo atendio una persona del equipo"
    if not any(m.role == "assistant" and MARCA_LINK_PRODUCTO in (m.content or "") for m in charla):
        return "no se le recomendo ningun producto"
    return None


# ── 2. ¿Compro? (Tienda Nube) ────────────────────────────────────────────────


async def _compro_en_la_web(telefono: str, desde: datetime) -> bool | None:
    """True si hay un pedido con ese celular desde "desde". None si no se pudo consultar."""
    from agent.tools import TIENDANUBE_ACCESS_TOKEN, TIENDANUBE_BASE_URL, TIENDANUBE_STORE_ID, _headers

    buscado = _digitos_finales(telefono)
    if not buscado or not TIENDANUBE_STORE_ID or not TIENDANUBE_ACCESS_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as cliente:
            r = await cliente.get(
                f"{TIENDANUBE_BASE_URL}/{TIENDANUBE_STORE_ID}/orders",
                headers=_headers(),
                params={
                    "created_at_min": desde.astimezone(timezone.utc).isoformat(),
                    "per_page": 200,
                    "fields": "id,contact_phone,billing_phone,customer,shipping_address",
                },
            )
    except httpx.HTTPError as e:
        logger.warning(f"Seguimiento: no se pudo consultar pedidos en Tienda Nube: {e}")
        return None
    if r.status_code == 404:  # Tienda Nube responde 404 cuando no hay pedidos en el rango
        return False
    if r.status_code != 200:
        logger.warning(f"Seguimiento: Tienda Nube respondio {r.status_code} al pedir pedidos")
        return None
    for pedido in r.json():
        telefonos = [
            pedido.get("contact_phone"),
            pedido.get("billing_phone"),
            (pedido.get("customer") or {}).get("phone"),
            (pedido.get("shipping_address") or {}).get("phone"),
        ]
        if any(_digitos_finales(t) == buscado for t in telefonos):
            return True
    return False


# ── 3. ¿Corresponde escribirle? (Claude) ─────────────────────────────────────

TAREA = """Tarea interna (no es un mensaje del cliente): esta es una charla de WhatsApp de hace casi un dia.
El cliente consulto por productos y le pasaste links, pero no hay una compra registrada en la web con su celular.

Decidí si corresponde mandarle UN mensaje de seguimiento para ayudarlo a concretar la compra.
NO corresponde si: dijo que ya compró (en la web o en el local) o que va a ir al local; dijo que no le interesa,
que lo piensa más adelante o que no lo busca ahora; la charla era un reclamo, un cambio, una devolución o el
seguimiento de un pedido; quedó molesto; pidió que no le escriban; o la charla ya cerró con una despedida clara
de que no va a comprar.

Si corresponde, escribí el mensaje: corto (1 o 2 oraciones), cálido y vendedor, con tu tono de siempre.
Nombrá el producto concreto que le interesó (sin inventar nada que no esté en la charla) y ofrecé ayudarlo con
lo que le haya frenado (talle, envío, cuotas). No repitas el link si ya se lo pasaste. No uses "che".
No menciones que es un mensaje automático ni que pasó un día.

Respondé SOLO con un JSON, sin texto alrededor:
{"enviar": true o false, "motivo": "en pocas palabras, por qué", "mensaje": "el texto, o vacío si no corresponde"}

Charla:
"""


def _transcribir(charla: list) -> str:
    lineas = []
    for m in charla:
        quien = "Cliente" if m.role == "user" else ("Equipo" if m.remitente == "humano" else "PAMPA")
        lineas.append(f"{quien}: {(m.content or '').strip()[:1200]}")
    return "\n".join(lineas)


def _leer_json(texto: str) -> dict | None:
    encontrado = re.search(r"\{.*\}", texto or "", re.DOTALL)
    if not encontrado:
        return None
    try:
        datos = json.loads(encontrado.group(0))
    except ValueError:
        return None
    return datos if isinstance(datos, dict) else None


async def _decidir(charla: list) -> dict | None:
    """{"enviar", "motivo", "mensaje"} o None si Claude fallo."""
    import agent.brain as brain

    try:
        respuesta = await brain.client.messages.create(
            model=brain.MODELO,
            max_tokens=1024,
            # El mismo system prompt (y la misma cache) que usa el bot: asi escribe con su tono
            system=[{"type": "text", "text": await brain.obtener_system_prompt_activo(), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": TAREA + _transcribir(charla)}],
            **({"output_config": {"effort": brain.ESFUERZO}} if brain.ESFUERZO else {}),
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Seguimiento: fallo la llamada a Claude: {e}")
        return None
    texto = "".join(b.text for b in respuesta.content if getattr(b, "type", "") == "text")
    datos = _leer_json(texto)
    if datos is None:
        logger.warning(f"Seguimiento: Claude no devolvio un JSON valido: {texto[:200]}")
    return datos


# ── 4. Ciclo ─────────────────────────────────────────────────────────────────


async def _registrar(telefono: str, mensaje_cliente_id: int, estado: str, motivo: str = "", mensaje: str = ""):
    async with async_session() as session:
        session.add(
            Seguimiento(
                telefono=telefono,
                mensaje_cliente_id=mensaje_cliente_id,
                estado=estado,
                motivo=motivo[:500],
                mensaje=mensaje[:2000],
                creado_en=datetime.now(timezone.utc),
            )
        )
        await session.commit()


async def revisar(proveedor, ahora: datetime | None = None) -> int:
    """Una pasada completa. Devuelve cuantos seguimientos se mandaron (o quedaron en borrador)."""
    ahora = ahora or datetime.now(timezone.utc)
    if not await esta_activo() or not _hora_permitida(ahora):
        return 0

    modo_envio = (os.getenv("MODO_ENVIO") or "borrador").strip().lower()
    mandados = 0
    for c in await _candidatos(ahora):
        lead, ultimo, charla = c["lead"], c["ultimo_cliente"], c["charla"]
        tel = lead.telefono

        motivo = _filtro_rapido(charla)
        if motivo:
            await _registrar(tel, ultimo.id, "descartado", motivo)
            continue

        compro = await _compro_en_la_web(tel, _utc(charla[0].timestamp) - timedelta(hours=1))
        if compro is None:
            continue  # sin poder verificar, mejor no escribir; se reintenta en la proxima pasada
        if compro:
            await _registrar(tel, ultimo.id, "descartado", "ya compró en la web")
            continue

        decision = await _decidir(charla)
        if decision is None:
            continue  # se reintenta en la proxima pasada
        mensaje = str(decision.get("mensaje") or "").strip()
        if not decision.get("enviar") or not mensaje:
            await _registrar(tel, ultimo.id, "descartado", str(decision.get("motivo") or "no corresponde"))
            continue

        if modo_envio == "borrador":
            await crear_borrador(tel, "(seguimiento de posible venta)", mensaje, "{}")
            await _registrar(tel, ultimo.id, "borrador", str(decision.get("motivo") or ""), mensaje)
            mandados += 1
            continue

        if await proveedor.enviar_mensaje(tel, mensaje, {}):
            await guardar_mensaje(tel, "assistant", mensaje)
            await _registrar(tel, ultimo.id, "enviado", str(decision.get("motivo") or ""), mensaje)
            mandados += 1
            logger.info(f"Seguimiento enviado a {tel}: {mensaje}")
        else:
            await _registrar(tel, ultimo.id, "error", "WhatsApp no aceptó el envío", mensaje)
    return mandados


async def mantener_activo(proveedor) -> None:
    """Tarea de fondo: revisa cada 10 minutos. Nunca debe morir por un error puntual."""
    while True:
        await asyncio.sleep(INTERVALO)
        try:
            await revisar(proveedor)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Seguimiento: error en la revision: {e}")
