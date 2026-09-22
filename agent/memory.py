# agent/memory.py — Memoria de conversaciones
# Generado por AgentKit

"""
Guarda el historial de cada conversacion por numero de telefono, y lleva registro de
que eventos de webhook ya se atendieron.

SQLite en local, PostgreSQL en produccion.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import DateTime, Integer, String, Text, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()
logger = logging.getLogger("agentkit")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Railway entrega la URL de PostgreSQL con el esquema "postgresql://" (o "postgres://").
# SQLAlchemy en modo asincrono necesita que el driver sea explicito.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# En produccion, SQLite vive dentro del contenedor y el disco del contenedor es efimero:
# cada redespliegue borra el historial de todas las conversaciones. Avisarlo fuerte, porque
# el agente arranca igual y el problema recien se nota cuando un cliente vuelve a escribir.
if DATABASE_URL.startswith("sqlite") and os.getenv("ENVIRONMENT") == "production":
    logger.warning(
        "Estas en produccion con SQLite. El historial se va a borrar en cada redespliegue. "
        "Agrega PostgreSQL y configura DATABASE_URL para que el agente recuerde a sus clientes."
    )

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def ahora() -> datetime:
    """Hora actual en UTC, con zona horaria."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Un mensaje del historial de conversacion."""

    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class EventoProcesado(Base):
    """
    Eventos de webhook que ya se atendieron.

    Los proveedores entregan "al menos una vez": el mismo evento puede llegar dos veces.
    Sin esta tabla, el cliente recibiria la misma respuesta repetida.
    """

    __tablename__ = "eventos_procesados"

    evento_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


class Lead(Base):
    """
    CRM minimo: un renglon por telefono que escribio alguna vez.

    "escalado" es lo que hace que el agente deje de contestar ese numero: una vez en
    True, main.py ya no lo vuelve a poner en False solo — lo reactiva una persona
    (ver scripts/leads.py).
    """

    __tablename__ = "leads"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    ultimo_mensaje: Mapped[str] = mapped_column(Text)
    veces_contactado: Mapped[int] = mapped_column(Integer, default=1)
    escalado: Mapped[bool] = mapped_column(default=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class Borrador(Base):
    """
    Una respuesta que el agente redacto pero todavia no mando.

    Modo borrador (el default): el agente redacta, guarda acá y espera. Nada le llega
    al cliente hasta que alguien lo aprueba con scripts/bandeja.py.
    """

    __tablename__ = "borradores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    mensaje_cliente: Mapped[str] = mapped_column(Text)
    respuesta: Mapped[str] = mapped_column(Text)
    # Serializado como JSON: lo que el proveedor necesita para poder enviar despues
    # (para Zernio, conversation_id/account_id; Meta no necesita nada).
    contexto_json: Mapped[str] = mapped_column(Text, default="{}")
    estado: Mapped[str] = mapped_column(String(20), default="pendiente", index=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def marcar_evento_procesado(evento_id: str) -> bool:
    """
    Registra un evento. Retorna True si es nuevo, False si ya se habia procesado.

    La unicidad la garantiza la base de datos (clave primaria), no una consulta previa:
    asi dos webhooks que llegan al mismo tiempo no pasan los dos.
    """
    if not evento_id:
        return True  # sin id no podemos deduplicar: se procesa

    async with async_session() as session:
        session.add(EventoProcesado(evento_id=evento_id, creado_en=ahora()))
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def liberar_evento(evento_id: str):
    """
    Borra la marca de un evento para que el reintento del proveedor SI se procese.

    Se usa cuando el mensaje se marco como procesado pero despues fallo el envio de la
    respuesta. Sin esto, el reintento se descartaria por duplicado y el cliente se
    quedaria sin respuesta para siempre.
    """
    if not evento_id:
        return
    async with async_session() as session:
        await session.execute(delete(EventoProcesado).where(EventoProcesado.evento_id == evento_id))
        await session.commit()


async def limpiar_eventos_viejos(dias: int = 7):
    """Borra los eventos de hace mas de N dias para que la tabla no crezca sin fin."""
    limite = ahora() - timedelta(days=dias)
    async with async_session() as session:
        resultado = await session.execute(
            delete(EventoProcesado).where(EventoProcesado.creado_en < limite)
        )
        await session.commit()
    if resultado.rowcount:
        logger.info(f"Se limpiaron {resultado.rowcount} eventos de mas de {dias} dias")


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de esa conversacion."""
    async with async_session() as session:
        session.add(Mensaje(telefono=telefono, role=role, content=content, timestamp=ahora()))
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Devuelve los ultimos N mensajes de una conversacion, en orden cronologico.

    Se ordena por id y no por timestamp: dos mensajes guardados en el mismo instante
    tienen el mismo timestamp, y el orden entre ellos quedaria librado al azar.
    """
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.id.desc())
            .limit(limite)
        )
        mensajes = list(resultado.scalars().all())

    mensajes.reverse()  # vienen del mas nuevo al mas viejo: los damos vuelta

    # La API de Claude exige que el historial empiece con un mensaje del usuario.
    # Si por un error anterior quedo un "assistant" suelto al principio, lo sacamos.
    while mensajes and mensajes[0].role != "user":
        mensajes.pop(0)

    return [{"role": m.role, "content": m.content} for m in mensajes]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversacion."""
    async with async_session() as session:
        await session.execute(delete(Mensaje).where(Mensaje.telefono == telefono))
        await session.commit()


# ── CRM: leads ───────────────────────────────────────────────────────────────


async def registrar_contacto(telefono: str, mensaje: str) -> Lead:
    """
    Crea o actualiza el lead de ese telefono con su ultimo mensaje.

    Se llama en CADA mensaje entrante, este o no escalado: es lo que le permite al
    dueno del negocio ver en scripts/leads.py quien escribio y que dijo, sin tener
    que entrar a WhatsApp.
    """
    async with async_session() as session:
        lead = await session.get(Lead, telefono)
        if lead is None:
            lead = Lead(telefono=telefono, ultimo_mensaje=mensaje, veces_contactado=1)
            session.add(lead)
        else:
            lead.ultimo_mensaje = mensaje
            lead.veces_contactado += 1
            lead.actualizado_en = ahora()
        await session.commit()
        await session.refresh(lead)
        return lead


async def esta_escalado(telefono: str) -> bool:
    """True si ese telefono ya paso a un humano: el agente no le vuelve a contestar."""
    async with async_session() as session:
        lead = await session.get(Lead, telefono)
        return bool(lead and lead.escalado)


async def marcar_escalado(telefono: str):
    """Marca el lead como escalado. Desde aca el agente deja de contestarle."""
    async with async_session() as session:
        lead = await session.get(Lead, telefono)
        if lead is not None:
            lead.escalado = True
            lead.actualizado_en = ahora()
            await session.commit()


async def reactivar_lead(telefono: str):
    """Vuelve a habilitar al agente para contestarle a ese telefono. Lo usa una persona a mano."""
    async with async_session() as session:
        lead = await session.get(Lead, telefono)
        if lead is not None:
            lead.escalado = False
            lead.actualizado_en = ahora()
            await session.commit()


async def listar_leads(limite: int = 50) -> list[Lead]:
    """Los leads mas recientes primero, para revisar quien escribio."""
    async with async_session() as session:
        resultado = await session.execute(
            select(Lead).order_by(Lead.actualizado_en.desc()).limit(limite)
        )
        return list(resultado.scalars().all())


async def obtener_conversacion_completa(telefono: str, limite: int = 200) -> list[dict]:
    """
    La conversacion entera para mostrarla en el panel, con fecha y hora.

    Distinta de obtener_historial(), que es la que alimenta al modelo: esa recorta a
    los ultimos mensajes, saca los "assistant" sueltos del principio porque la API lo
    exige, y no devuelve timestamps. Para mirar un chat hace falta lo contrario:
    todo lo que paso, tal cual paso, con la hora de cada mensaje.
    """
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.id.desc())
            .limit(limite)
        )
        mensajes = list(resultado.scalars().all())

    mensajes.reverse()
    return [
        {
            "role": m.role,
            "content": m.content,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        }
        for m in mensajes
    ]


# ── Modo borrador ──────────────────────────────────────────────────────────


async def crear_borrador(telefono: str, mensaje_cliente: str, respuesta: str, contexto_json: str) -> int:
    """Guarda una respuesta redactada, pendiente de aprobacion. Devuelve su id."""
    async with async_session() as session:
        borrador = Borrador(
            telefono=telefono,
            mensaje_cliente=mensaje_cliente,
            respuesta=respuesta,
            contexto_json=contexto_json,
            estado="pendiente",
        )
        session.add(borrador)
        await session.commit()
        await session.refresh(borrador)
        return borrador.id


async def listar_borradores_pendientes() -> list[Borrador]:
    """Los borradores que todavia nadie aprobo ni descarto, mas viejo primero."""
    async with async_session() as session:
        resultado = await session.execute(
            select(Borrador).where(Borrador.estado == "pendiente").order_by(Borrador.id.asc())
        )
        return list(resultado.scalars().all())


async def marcar_borrador(borrador_id: int, estado: str):
    """Pasa un borrador a 'aprobado' o 'descartado'."""
    async with async_session() as session:
        borrador = await session.get(Borrador, borrador_id)
        if borrador is not None:
            borrador.estado = estado
            await session.commit()
