# agent/catalogo.py — Copia en memoria del catalogo de Tienda Nube

"""
Tienda Nube busca solo por nombre y etiquetas, y el COLOR no esta en el nombre: es un valor
de cada variante. Para poder contestar "¿tenes algo en azul?" hay que mirar las variantes de
todo el catalogo, y bajar 1.500 productos en cada consulta no se puede (son ~30 segundos y
~36 MB). Por eso se guarda una copia compacta en memoria y se busca ahi.

Como se mantiene al dia:
- Al arrancar se baja el catalogo completo (en segundo plano: el bot responde igual mientras
  tanto, con la busqueda en vivo de siempre).
- Cada 10 minutos se piden solo los productos que cambiaron (updated_at_min): es liviano y
  deja el stock y los colores casi en tiempo real.
- Cada 3 horas se vuelve a bajar todo, para sacar de la copia lo que se despublico o borro,
  que la consulta incremental no puede avisar.

Aun asi la copia puede estar unos minutos atrasada: el bot confirma el stock exacto con
obtener_detalle_producto (en vivo) antes de asegurar algo.
"""

import asyncio
import logging
import math
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import httpx

from agent.tools import (
    TIENDANUBE_ACCESS_TOKEN,
    TIENDANUBE_BASE_URL,
    TIENDANUBE_STORE_ID,
    _PALABRAS_VACIAS,
    _headers,
    _texto_o_vacio,
    _valor_variante,
    _variante_disponible,
)

logger = logging.getLogger("agentkit")

INTERVALO_INCREMENTAL = 10 * 60
INTERVALO_COMPLETO = 3 * 60 * 60
_MARGEN_INCREMENTAL = timedelta(minutes=15)  # se pide un poco de mas para no perder cambios en el borde
_REINTENTOS = 3
_PRODUCTOS_POR_PAGINA = 200

_productos: dict = {}  # id -> producto compacto
_estado: dict = {"completo_en": None, "actualizado_en": None, "ultimo_error": None}


def _norm(texto: str) -> str:
    """Minusculas y sin tildes, para comparar 'Niña'/'nina', 'Náutico'/'nautico'."""
    sin_tildes = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    return sin_tildes.lower()


def _compactar(p: dict) -> dict:
    """
    Se queda con lo justo de un producto, con la MISMA forma que devuelve la API de Tienda
    Nube en los campos que usan tools._linea_producto y los helpers de color: asi la misma
    funcion arma la linea para un producto de la copia o uno traido en vivo.
    """
    imagenes = [i for i in (p.get("images") or []) if i.get("src")]
    principal = min(imagenes, key=lambda i: i.get("position") or 99) if imagenes else None
    variantes = [
        {
            "price": v.get("price"),
            "stock": v.get("stock"),
            "stock_management": v.get("stock_management", True),
            "values": v.get("values") or [],
        }
        for v in (p.get("variants") or [])
    ]
    texto_busqueda = " ".join(
        [
            _texto_o_vacio(p.get("name")),
            p.get("brand") or "",
            _texto_o_vacio(p.get("tags")) if isinstance(p.get("tags"), (dict, str)) else "",
            # La descripcion suele nombrar rasgos que el titulo no (stiletto, plataforma, altura de taco).
            re.sub(r"<[^>]+>", " ", _texto_o_vacio(p.get("description")))[:700],
        ]
    )
    return {
        "id": p["id"],
        "name": p.get("name"),
        "brand": p.get("brand"),
        "canonical_url": p.get("canonical_url"),
        "attributes": p.get("attributes") or [],
        "variants": variantes,
        "images": [{"src": principal["src"], "position": principal.get("position")}] if principal else [],
        "_texto": _norm(texto_busqueda),
    }


async def _pedir(cliente: httpx.AsyncClient, params: dict) -> list[dict] | None:
    """
    Una pagina de GET /products, con reintentos: bajar el catalogo son ~8 llamadas largas y
    la API a veces corta la conexion a mitad. Devuelve [] cuando ya no hay mas paginas (404)
    y None si despues de los reintentos sigue fallando.
    """
    url = f"{TIENDANUBE_BASE_URL}/{TIENDANUBE_STORE_ID}/products"
    for intento in range(1, _REINTENTOS + 1):
        try:
            r = await cliente.get(url, params=params, headers=_headers())
        except httpx.HTTPError as e:
            logger.warning(f"Catalogo: error de red (intento {intento}/{_REINTENTOS}): {type(e).__name__}")
        else:
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return []  # "Last page is N": no hay mas paginas / ningun cambio
            logger.warning(f"Catalogo: Tienda Nube respondio {r.status_code} (intento {intento}/{_REINTENTOS})")
        await asyncio.sleep(2 * intento)
    return None


async def _bajar(params_extra: dict) -> list[dict] | None:
    """Todas las paginas de productos publicados con esos filtros, o None si alguna fallo."""
    productos: list[dict] = []
    async with httpx.AsyncClient(timeout=60.0) as cliente:
        pagina = 1
        while True:
            lote = await _pedir(
                cliente,
                {"published": "true", "per_page": _PRODUCTOS_POR_PAGINA, "page": pagina, **params_extra},
            )
            if lote is None:
                return None
            productos.extend(_compactar(p) for p in lote)
            if len(lote) < _PRODUCTOS_POR_PAGINA:
                return productos
            pagina += 1


async def sincronizar_completo() -> bool:
    inicio = datetime.now(timezone.utc)
    t = time.time()
    nuevos = await _bajar({})
    if nuevos is None:
        _estado["ultimo_error"] = "no se pudo bajar el catalogo completo"
        return False
    # Se reemplaza todo de una: asi lo despublicado o borrado sale de la copia.
    _productos.clear()
    _productos.update({p["id"]: p for p in nuevos})
    _estado.update(completo_en=inicio, actualizado_en=inicio, ultimo_error=None)
    logger.info(f"Catalogo completo: {len(_productos)} productos en {time.time() - t:.0f}s")
    return True


async def sincronizar_incremental() -> bool:
    desde = (_estado["actualizado_en"] or datetime.now(timezone.utc)) - _MARGEN_INCREMENTAL
    inicio = datetime.now(timezone.utc)
    cambiados = await _bajar({"updated_at_min": desde.strftime("%Y-%m-%dT%H:%M:%S+00:00")})
    if cambiados is None:
        _estado["ultimo_error"] = "no se pudo actualizar el catalogo"
        return False
    for p in cambiados:
        _productos[p["id"]] = p
    _estado.update(actualizado_en=inicio, ultimo_error=None)
    if cambiados:
        logger.info(f"Catalogo: {len(cambiados)} productos actualizados")
    return True


async def mantener_actualizado() -> None:
    """Tarea de fondo: carga inicial, y despues refrescos incrementales y completos periodicos."""
    if not TIENDANUBE_STORE_ID or not TIENDANUBE_ACCESS_TOKEN:
        logger.warning("Catalogo: falta la conexion con Tienda Nube, no se arma la copia en memoria")
        return
    while not await sincronizar_completo():
        await asyncio.sleep(60)
    while True:
        await asyncio.sleep(INTERVALO_INCREMENTAL)
        try:
            completo_en = _estado["completo_en"]
            if completo_en and datetime.now(timezone.utc) - completo_en >= timedelta(seconds=INTERVALO_COMPLETO):
                await sincronizar_completo()
            else:
                await sincronizar_incremental()
        except Exception as e:  # noqa: BLE001 — la tarea de fondo nunca debe morir por un error puntual
            logger.exception(f"Catalogo: error actualizando la copia: {e}")


def listo() -> bool:
    return bool(_productos)


def resumen() -> dict:
    """Para el health check: si la copia esta cargada y de cuando es."""
    actualizado = _estado["actualizado_en"]
    return {
        "productos": len(_productos),
        "actualizado": actualizado.isoformat() if actualizado else None,
        "error": _estado["ultimo_error"],
    }


# ── Busqueda ─────────────────────────────────────────────────────────────────


def _tokens(consulta: str, excluir: set[str]) -> list[str]:
    """Palabras utiles de la consulta, normalizadas y en singular simple (sandalias -> sandalia)."""
    vacias = {_norm(w) for w in _PALABRAS_VACIAS}
    salida = []
    for palabra in re.findall(r"[a-z0-9]+", _norm(consulta)):
        if palabra in vacias or len(palabra) <= 2 or palabra in excluir:
            continue
        salida.append(palabra[:-1] if len(palabra) > 3 and palabra.endswith("s") else palabra)
    return list(dict.fromkeys(salida))


def _variantes_del_color(p: dict, color: str) -> list[dict]:
    """Variantes con stock cuyo color empieza con 'color' o lo contiene como palabra ('azul' -> 'Azul Marino')."""
    patron = re.compile(rf"\b{re.escape(color)}")
    return [
        v
        for v in p["variants"]
        if _variante_disponible(v) and patron.search(_norm(_valor_variante(p, v, "color")))
    ]


def buscar(consulta: str, color: str | None = None, limite: int = 10) -> list[dict] | None:
    """
    Productos de la copia que coinciden con 'consulta' (palabras en nombre, marca, etiquetas
    o descripcion) y, si se pide, tienen ese color CON STOCK. None si la copia todavia no
    cargo, para que el llamador use la busqueda en vivo.

    Se piden al menos el 60% de las palabras: el cliente habla en lenguaje natural y el
    catalogo no siempre usa las mismas (ej. "taco alto" vs "taco aguja").
    """
    if not _productos:
        return None

    color_norm = _norm(color).strip() if color else None
    color_norm = color_norm[:-1] if color_norm and len(color_norm) > 4 and color_norm.endswith("s") else color_norm
    palabras = _tokens(consulta, {color_norm} if color_norm else set())
    minimo = max(1, math.ceil(0.6 * len(palabras))) if palabras else 0

    resultados = []
    for p in _productos.values():
        puntaje = sum(1 for w in palabras if w in p["_texto"])
        if palabras and puntaje < minimo:
            continue
        if color_norm:
            variantes = _variantes_del_color(p, color_norm)
            if not variantes:
                continue
        resultados.append((puntaje, p))

    resultados.sort(key=lambda x: (-x[0], _texto_o_vacio(x[1].get("name"))))
    return [p for _, p in resultados[:limite]]
