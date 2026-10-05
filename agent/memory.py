# agent/memory.py — Memoria de conversaciones
# Generado por AgentKit

"""
Guarda el historial de cada conversacion por numero de telefono, y lleva registro de
que eventos de webhook ya se atendieron.

SQLite en local, PostgreSQL en produccion.
"""

import base64
import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import DateTime, Float, Integer, LargeBinary, String, Text, delete, func, inspect, select, text
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
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant" (lo que ve la API de Claude)
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    # Quien REDACTO el mensaje, para el panel: "cliente" | "bot" | "humano". Distinto de
    # "role": un mensaje "humano" (alguien del equipo respondiendo a mano desde el panel)
    # sigue siendo role="assistant" para la API de Claude, pero remitente="humano" para
    # que el panel lo pinte distinto de una respuesta de Fran. Nullable porque los
    # mensajes guardados ANTES de este campo no lo tienen: para esos, el panel infiere
    # "cliente"/"bot" a partir de "role" (ver obtener_conversacion_completa).
    remitente: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    # Foto que mando el cliente en ESTE mensaje (ver clase Imagen). Nullable: casi todos
    # los mensajes son solo texto, y los viejos (anteriores a este campo) tampoco la tienen.
    imagen_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)


class Imagen(Base):
    """
    Foto que mando un cliente, guardada para poder verla en el panel.

    Se guarda al recibirla y no se vuelve a pedir a Meta despues: los links de descarga
    de WhatsApp vencen a los pocos dias, asi que si no se persiste en el momento, la foto
    se pierde para siempre. Va en la base (no en archivos sueltos) para que viaje igual con
    SQLite sobre el volumen de Railway o con Postgres, sin depender de ninguna carpeta.
    """

    __tablename__ = "imagenes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    media_type: Mapped[str] = mapped_column(String(50))
    datos: Mapped[bytes] = mapped_column(LargeBinary)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


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
    # Distinto de "escalado": escalado es un AVISO (el bot le pidio a una persona que
    # se sume, pero Fran sigue contestando). "bot_activo" es un APAGADOR manual: alguien
    # del equipo lo pone en False desde el panel cuando quiere atender ese numero en
    # persona, y ahi si el bot deja de responder ese numero hasta que lo reactiven. Son
    # dos cosas ortogonales: puede estar escalado y con el bot activo (avisaron a una
    # persona pero Fran sigue mientras tanto), o con el bot apagado sin estar escalado
    # (alguien tomo la charla directamente, sin pasar por el flujo de escalacion).
    bot_activo: Mapped[bool] = mapped_column(default=True)
    # Cuando se mando el ultimo aviso interno por este telefono (no cuando se marco
    # escalado por primera vez: son la misma columna porque hoy se actualizan siempre
    # juntas). Sirve para decidir si conviene volver a avisar — ver
    # debe_reavisar_escalacion.
    ultimo_aviso_escalado: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class Uso(Base):
    """
    Consumo de Claude por cada respuesta real del agente.

    Una fila por mensaje de cliente contestado (no por llamada HTTP a Anthropic): si un
    mensaje disparo varios pasos de herramientas, esta fila ya trae el total acumulado
    de esa tanda. Sirve para el dashboard de metricas (agent/panel.py).
    """

    __tablename__ = "uso"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    modelo: Mapped[str] = mapped_column(String(50))
    tokens_entrada: Mapped[int] = mapped_column(Integer, default=0)
    tokens_salida: Mapped[int] = mapped_column(Integer, default=0)
    tokens_cache: Mapped[int] = mapped_column(Integer, default=0)
    pasos_herramientas: Mapped[int] = mapped_column(Integer, default=0)
    costo_usd: Mapped[float] = mapped_column(Float, default=0.0)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


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


class Configuracion(Base):
    """
    Pares clave/valor editables desde el panel sin tocar codigo ni redeployar.

    Hoy solo se usa para "system_prompt" (ver agent/panel.py, seccion Prompt), pero
    queda generico por si mas adelante hace falta guardar otro ajuste del mismo tipo.
    Va en la base y no en config/prompts.yaml porque en produccion el filesystem del
    contenedor es efimero: un cambio guardado solo en el archivo se pierde en el
    proximo redespliegue, mientras que esto sobrevive en Postgres.
    """

    __tablename__ = "configuracion"

    clave: Mapped[str] = mapped_column(String(100), primary_key=True)
    valor: Mapped[str] = mapped_column(Text)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


# Columnas agregadas a tablas que ya existian de deploys anteriores. Cada entrada:
# (tabla, columna, tipo SQL, "DEFAULT ..." o "" si no hace falta). El default en la
# propia sentencia ALTER hace que las filas VIEJAS tambien queden con un valor valido
# (ej. leads.bot_activo=true), no solo las nuevas -- eso es lo que create_all() no
# puede hacer por una tabla que ya existia.
_COLUMNAS_NUEVAS = [
    ("leads", "ultimo_aviso_escalado", {"postgresql": "TIMESTAMP WITH TIME ZONE", "*": "TIMESTAMP"}, ""),
    ("leads", "bot_activo", {"postgresql": "BOOLEAN", "*": "BOOLEAN"}, "DEFAULT TRUE"),
    ("mensajes", "remitente", {"postgresql": "VARCHAR(20)", "*": "VARCHAR(20)"}, ""),
    ("mensajes", "imagen_id", {"postgresql": "INTEGER", "*": "INTEGER"}, ""),
]


def _migrar_columnas_nuevas(conn):
    """
    create_all() solo crea tablas que faltan: NO agrega columnas nuevas a una tabla que
    ya existia de un deploy anterior. Sin esto, agregar un campo a un modelo (como
    "ultimo_aviso_escalado" en Lead, en su momento) rompe en produccion con un error de
    "columna no existe" en la primera consulta que la use, contra una base que ya tenia
    esa tabla de antes.
    """
    inspector = inspect(conn)
    tablas_existentes = set(inspector.get_table_names())
    columnas_por_tabla: dict[str, set[str]] = {}

    for tabla, columna, tipos, default_sql in _COLUMNAS_NUEVAS:
        if tabla not in tablas_existentes:
            continue  # tabla recien creada por create_all(): ya tiene todas las columnas
        if tabla not in columnas_por_tabla:
            columnas_por_tabla[tabla] = {c["name"] for c in inspector.get_columns(tabla)}
        if columna in columnas_por_tabla[tabla]:
            continue
        tipo = tipos.get(conn.dialect.name, tipos["*"])
        conn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo} {default_sql}".strip()))
        logger.info(f"Migracion: agregada la columna {tabla}.{columna}")


async def inicializar_db():
    """Crea las tablas si no existen, y agrega columnas nuevas a tablas que ya estaban."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrar_columnas_nuevas)


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


async def guardar_mensaje(
    telefono: str,
    role: str,
    content: str,
    remitente: str | None = None,
    imagen_id: int | None = None,
):
    """
    Guarda un mensaje en el historial de esa conversacion.

    "remitente" es para el panel ("cliente"/"bot"/"humano"), no para la API de Claude.
    Si no se pasa, se infiere del "role" de siempre (user->cliente, assistant->bot): la
    inmensa mayoria de los llamadores no necesitan tocar esto, solo lo pasa explicito el
    endpoint del panel que manda un mensaje escrito a mano por una persona del equipo.
    """
    if remitente is None:
        remitente = "cliente" if role == "user" else "bot"
    async with async_session() as session:
        session.add(
            Mensaje(
                telefono=telefono,
                role=role,
                content=content,
                remitente=remitente,
                imagen_id=imagen_id,
                timestamp=ahora(),
            )
        )
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


async def guardar_imagen(telefono: str, media_type: str, datos_base64: str) -> int:
    """Guarda una foto del cliente (llega en base64, tal como se la manda a Claude). Devuelve su id."""
    async with async_session() as session:
        imagen = Imagen(
            telefono=telefono,
            media_type=media_type,
            datos=base64.b64decode(datos_base64),
            creado_en=ahora(),
        )
        session.add(imagen)
        await session.commit()
        await session.refresh(imagen)
        return imagen.id


async def obtener_imagen(imagen_id: int) -> tuple[str, bytes] | None:
    """(media_type, bytes) de una foto guardada, o None si no existe."""
    async with async_session() as session:
        imagen = await session.get(Imagen, imagen_id)
        return (imagen.media_type, imagen.datos) if imagen else None


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
    """
    Marca el lead como escalado y registra el momento del aviso.

    El momento se guarda ACA, no en un lugar aparte, porque hoy los dos llamadores
    (main.py y escalacion.py) llaman a esto siempre justo antes de mandar el aviso
    interno: son, en la practica, el mismo evento. Ese timestamp es lo que despues usa
    debe_reavisar_escalacion para decidir si ya paso suficiente tiempo como para
    mandar el aviso de nuevo.
    """
    async with async_session() as session:
        lead = await session.get(Lead, telefono)
        if lead is not None:
            lead.escalado = True
            lead.ultimo_aviso_escalado = ahora()
            lead.actualizado_en = ahora()
            await session.commit()


async def debe_reavisar_escalacion(telefono: str, cooldown: timedelta) -> bool:
    """
    True si corresponde mandar un aviso interno para este telefono: nunca se escalo
    antes, o paso mas del "cooldown" desde el ultimo aviso.

    Sin esto, cada mensaje que repite la misma palabra clave (o cada vez que el agente
    vuelve a usar la herramienta escalar_a_humano) mandaria un aviso nuevo al local, lo
    cual satura de notificaciones repetidas por la MISMA gestion. Pero si el cliente
    insiste despues de un rato, es una señal real de que el primer aviso se paso por
    alto, asi que conviene avisar de nuevo en vez de asumir que ya esta cubierto.
    """
    async with async_session() as session:
        lead = await session.get(Lead, telefono)
        if lead is None or not lead.escalado or lead.ultimo_aviso_escalado is None:
            return True
        ultimo_aviso = lead.ultimo_aviso_escalado
        # SQLite no guarda la zona horaria: lo que vuelve es "naive" aunque la columna
        # sea DateTime(timezone=True) y lo que se guardo (ahora()) si la tuviera. Sin
        # esto, restar contra un datetime "aware" tira TypeError. En Postgres esto no
        # hace falta (ya vuelve con tzinfo), pero no molesta si ya la tiene.
        if ultimo_aviso.tzinfo is None:
            ultimo_aviso = ultimo_aviso.replace(tzinfo=timezone.utc)
        return ahora() - ultimo_aviso > cooldown


async def reactivar_lead(telefono: str):
    """Vuelve a habilitar al agente para contestarle a ese telefono. Lo usa una persona a mano."""
    async with async_session() as session:
        lead = await session.get(Lead, telefono)
        if lead is not None:
            lead.escalado = False
            lead.actualizado_en = ahora()
            await session.commit()


async def resolver_escalado(telefono: str) -> bool:
    """
    Saca la etiqueta "espera persona" de un lead ya atendido (boton rapido del panel,
    para cuando alguien le contesto a mano por fuera del flujo de escalar_a_humano).

    A diferencia de reactivar_lead, NO toca "actualizado_en": esa fecha representa el
    ultimo contacto real del cliente, y la usa el filtro de fecha de la lista de chats
    (ver agent/panel.py). Si la pisara con "ahora", resolver un chat de hace tres dias
    lo haria aparecer como si el cliente hubiera escrito hoy.

    Devuelve False si el telefono no tiene lead (nunca escribio), para que el llamador
    pueda avisar.
    """
    async with async_session() as session:
        lead = await session.get(Lead, telefono)
        if lead is None:
            return False
        lead.escalado = False
        await session.commit()
        return True


async def alternar_bot(telefono: str, activo: bool) -> bool:
    """
    Prende o apaga el bot para UN numero puntual (panel: toggle "bot activo").

    A diferencia de reactivar_lead (que solo puede reactivar, nunca apagar), esto lo usa
    el panel para las dos direcciones: alguien del equipo toma la charla a mano (activo
    False) y despues, cuando termina, se la devuelve a Fran (activo True). Devuelve False
    si el telefono no tiene lead (nunca escribio), para que el llamador pueda avisar.
    """
    async with async_session() as session:
        lead = await session.get(Lead, telefono)
        if lead is None:
            return False
        lead.bot_activo = activo
        lead.actualizado_en = ahora()
        await session.commit()
        return True


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
            # Mensajes guardados antes de que existiera esta columna quedan en None: se
            # infiere lo mismo que hacia el panel antes (por "role"), asi que ese chat
            # viejo sigue viendose exactamente igual que siempre.
            "remitente": m.remitente or ("cliente" if m.role == "user" else "bot"),
            "imagen_id": m.imagen_id,
        }
        for m in mensajes
    ]


# ── Metricas (dashboard) ───────────────────────────────────────────────────


async def guardar_uso(
    telefono: str,
    modelo: str,
    tokens_entrada: int,
    tokens_salida: int,
    tokens_cache: int,
    pasos_herramientas: int,
    costo_usd: float,
):
    """Registra el consumo de UNA respuesta real del agente (ver clase Uso)."""
    async with async_session() as session:
        session.add(
            Uso(
                telefono=telefono,
                modelo=modelo,
                tokens_entrada=tokens_entrada,
                tokens_salida=tokens_salida,
                tokens_cache=tokens_cache,
                pasos_herramientas=pasos_herramientas,
                costo_usd=costo_usd,
                creado_en=ahora(),
            )
        )
        await session.commit()


def _inicio_del_dia() -> datetime:
    """Medianoche de hoy, en UTC. La base guarda todo en UTC; es una aproximacion
    simple y consistente, aunque no coincida exacto con la medianoche en Argentina."""
    return ahora().replace(hour=0, minute=0, second=0, microsecond=0)


async def obtener_metricas(dias_serie: int = 7) -> dict:
    """
    Junta todo lo que muestra el dashboard en una sola consulta por tabla: mensajes
    entrantes/salientes, leads, escalados, consumo de Claude (tokens y costo estimado),
    y una serie de los ultimos N dias de mensajes entrantes para el grafico.
    """
    inicio_hoy = _inicio_del_dia()
    desde_serie = inicio_hoy - timedelta(days=dias_serie - 1)

    async with async_session() as session:

        async def _contar(modelo, *condiciones) -> int:
            return await session.scalar(select(func.count()).select_from(modelo).where(*condiciones)) or 0

        mensajes_cliente_total = await _contar(Mensaje, Mensaje.role == "user")
        mensajes_bot_total = await _contar(Mensaje, Mensaje.role == "assistant")
        mensajes_cliente_hoy = await _contar(Mensaje, Mensaje.role == "user", Mensaje.timestamp >= inicio_hoy)
        mensajes_bot_hoy = await _contar(Mensaje, Mensaje.role == "assistant", Mensaje.timestamp >= inicio_hoy)

        leads_total = await _contar(Lead)
        leads_hoy = await _contar(Lead, Lead.creado_en >= inicio_hoy)
        escalados_activos = await _contar(Lead, Lead.escalado.is_(True))
        escalados_hoy = await _contar(Lead, Lead.escalado.is_(True), Lead.ultimo_aviso_escalado >= inicio_hoy)

        async def _uso(*condiciones) -> dict:
            fila = await session.execute(
                select(
                    func.coalesce(func.sum(Uso.tokens_entrada), 0),
                    func.coalesce(func.sum(Uso.tokens_salida), 0),
                    func.coalesce(func.sum(Uso.tokens_cache), 0),
                    func.coalesce(func.sum(Uso.costo_usd), 0.0),
                    func.count(),
                ).where(*condiciones)
            )
            te, ts, tc, costo, respuestas = fila.one()
            return {
                "tokens_entrada": int(te),
                "tokens_salida": int(ts),
                "tokens_cache": int(tc),
                "costo_usd": round(float(costo), 4),
                "respuestas": int(respuestas),
            }

        uso_total = await _uso()
        uso_hoy = await _uso(Uso.creado_en >= inicio_hoy)

        filas_serie = await session.execute(
            select(func.date(Mensaje.timestamp), func.count())
            .where(Mensaje.role == "user", Mensaje.timestamp >= desde_serie)
            .group_by(func.date(Mensaje.timestamp))
        )
        # func.date() en SQLite da un string "YYYY-MM-DD"; en Postgres da un date().
        # str(...)[:10] normaliza los dos casos al mismo formato de clave.
        conteos = {str(dia)[:10]: cantidad for dia, cantidad in filas_serie.all()}
        serie = []
        for i in range(dias_serie):
            dia = (desde_serie + timedelta(days=i)).date().isoformat()
            serie.append({"dia": dia, "mensajes": conteos.get(dia, 0)})

    return {
        "mensajes": {
            "cliente_hoy": mensajes_cliente_hoy,
            "cliente_total": mensajes_cliente_total,
            "bot_hoy": mensajes_bot_hoy,
            "bot_total": mensajes_bot_total,
        },
        "leads": {"total": leads_total, "hoy": leads_hoy},
        "escalados": {"activos": escalados_activos, "hoy": escalados_hoy},
        "uso_hoy": uso_hoy,
        "uso_total": uso_total,
        "serie_mensajes": serie,
    }


# ── Configuracion editable (ej: system prompt) ──────────────────────────────


async def obtener_config(clave: str) -> str | None:
    """El valor guardado para esa clave, o None si nunca se seteo (usar el default del yaml)."""
    async with async_session() as session:
        fila = await session.get(Configuracion, clave)
        return fila.valor if fila else None


async def guardar_config(clave: str, valor: str):
    """Crea o pisa el valor de una clave."""
    async with async_session() as session:
        fila = await session.get(Configuracion, clave)
        if fila is None:
            session.add(Configuracion(clave=clave, valor=valor, actualizado_en=ahora()))
        else:
            fila.valor = valor
            fila.actualizado_en = ahora()
        await session.commit()


async def borrar_config(clave: str):
    """Saca el override: la proxima lectura vuelve a caer al default del yaml."""
    async with async_session() as session:
        await session.execute(delete(Configuracion).where(Configuracion.clave == clave))
        await session.commit()


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
