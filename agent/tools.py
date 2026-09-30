# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas especificas de PAMPA SHOP.

A diferencia del template base de AgentKit, estas SI se ejecutan solas: PAMPA SHOP
pidio que el agente conteste con datos reales y actualizados (stock, precio, talles)
en vez de tener esa info fija en el system prompt, asi que brain.py las conecta al
ciclo de tool use de Claude (ver TOOLS y _ejecutar_herramienta en brain.py).

Todas las funciones son de solo lectura (scopes read_products, read_orders,
read_customers, read_content) — el agente nunca modifica nada en Tienda Nube.
"""

import logging
import os

import httpx
import yaml
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

TIENDANUBE_STORE_ID = os.getenv("TIENDANUBE_STORE_ID", "")
TIENDANUBE_ACCESS_TOKEN = os.getenv("TIENDANUBE_ACCESS_TOKEN", "")
TIENDANUBE_API_VERSION = os.getenv("TIENDANUBE_API_VERSION") or "2025-03"
TIENDANUBE_BASE_URL = f"https://api.tiendanube.com/{TIENDANUBE_API_VERSION}"

# La API de Tiendanube exige un User-Agent identificable en cada request, o responde
# 400 Bad Request. No usar un valor generico: si Tiendanube necesita contactarnos por
# un problema con la app, este es el dato que van a mirar.
_USER_AGENT = "PampaShop AgentKit WhatsApp Bot (ventas@pampashop.com.ar)"

if not TIENDANUBE_STORE_ID or not TIENDANUBE_ACCESS_TOKEN:
    logger.warning(
        "Faltan TIENDANUBE_STORE_ID o TIENDANUBE_ACCESS_TOKEN: el agente no va a poder "
        "consultar catalogo, stock ni precios en vivo."
    )


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {TIENDANUBE_ACCESS_TOKEN}",
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
    }


def _texto_o_vacio(campo) -> str:
    """
    Los campos de texto de Tiendanube vienen como {"es": "...", "pt": "...'} por el
    soporte multi-idioma. La tienda solo usa espanol, asi que nos quedamos con "es"
    y caemos a cualquier otro valor si por algun motivo faltara.
    """
    if isinstance(campo, dict):
        return campo.get("es") or next(iter(campo.values()), "") or ""
    return campo or ""


# Tiendanube exige que TODAS las palabras del parametro "q" aparezcan juntas en el
# nombre/tag/SKU. Un cliente escribe en lenguaje natural ("botas de hombre borcego"),
# pero el catalogo suele usar solo el termino tecnico ("Borcego Hombre..."), asi que
# la busqueda completa da cero resultados aunque el producto exista. Estas palabras se
# ignoran al armar la busqueda palabra por palabra de respaldo, porque no aportan nada
# para encontrar un producto puntual.
_PALABRAS_VACIAS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "para", "con",
    "y", "o", "en", "que", "tenes", "tienen", "hay", "quiero", "busco", "necesito",
    "algun", "alguna", "algo", "por", "favor",
}


async def _consultar_productos_tienda_nube(params: dict) -> list[dict] | None:
    """Llamada cruda a GET /products. Devuelve None si hubo un error de red o de la API."""
    url = f"{TIENDANUBE_BASE_URL}/{TIENDANUBE_STORE_ID}/products"
    try:
        async with httpx.AsyncClient(timeout=15.0) as cliente:
            r = await cliente.get(url, params=params, headers=_headers())
    except httpx.HTTPError as e:
        logger.error(f"Error de red consultando productos en Tienda Nube: {e}")
        return None

    if r.status_code != 200:
        logger.error(f"Tienda Nube rechazo la busqueda de productos [{r.status_code}]: {r.text[:300]}")
        return None

    return r.json()


async def buscar_productos_tienda_nube(consulta: str) -> str:
    """
    Busca productos en el catalogo real de PAMPA SHOP por nombre, tag o SKU.

    Devuelve una lista compacta (nombre, marca, id, rango de precio, si tiene stock)
    para que el modelo elija el producto correcto y despues pida el detalle completo
    con obtener_detalle_producto. No devuelve la descripcion completa aca a proposito,
    para no gastar de mas el contexto de la conversacion.
    """
    if not TIENDANUBE_STORE_ID or not TIENDANUBE_ACCESS_TOKEN:
        return "No puedo consultar el catalogo ahora mismo: falta la conexion con Tienda Nube."

    productos = await _consultar_productos_tienda_nube(
        {"q": consulta, "published": "true", "per_page": 10}
    )
    if productos is None:
        return "No pude consultar el catalogo ahora mismo. Probemos de nuevo en un momento."

    if not productos:
        # Respaldo: probamos palabra por palabra, porque el "q" de Tiendanube exige
        # que todas las palabras aparezcan juntas y el cliente no siempre usa el
        # termino exacto del catalogo (ej: "botas" en vez de "borcego").
        palabras = [p for p in consulta.lower().split() if p not in _PALABRAS_VACIAS and len(p) > 2]
        # Las palabras mas largas suelen ser las mas especificas del catalogo (marca,
        # tipo de calzado: "borcego", "vizzano") mientras que las cortas son genericas
        # ("bota", "nino"). Se consultan primero para que, al cortar a 10 resultados,
        # queden los mas relevantes en vez de los primeros que aparecieron por azar.
        palabras.sort(key=len, reverse=True)
        vistos: dict[str, dict] = {}
        for palabra in palabras:
            resultado = await _consultar_productos_tienda_nube(
                {"q": palabra, "published": "true", "per_page": 20}
            )
            for p in resultado or []:
                vistos[p["id"]] = p

        # El "q" de Tiendanube no filtra por genero de forma confiable (un producto de
        # nena puede aparecer buscando "borcego" aunque el cliente pidio "de hombre").
        # Si la consulta menciona un genero, nos quedamos solo con los productos cuyo
        # nombre lo confirma -- y si eso deja todo vacio, mostramos lo que haya en vez
        # de decir que no existe.
        _GENEROS = {
            "hombre": ("hombre",), "hombres": ("hombre",),
            "mujer": ("mujer", "dama"), "mujeres": ("mujer", "dama"), "dama": ("mujer", "dama"),
            "nino": ("nino", "niño"), "niño": ("nino", "niño"),
            "nina": ("nina", "niña"), "niña": ("nina", "niña"),
        }
        genero_pedido = next((g for p in palabras for g in _GENEROS.get(p, ())), None)
        candidatos = list(vistos.values())
        if genero_pedido:
            filtrados = [
                p for p in candidatos if genero_pedido in _texto_o_vacio(p.get("name")).lower()
            ]
            if filtrados:
                candidatos = filtrados

        productos = candidatos[:10]

    if not productos:
        return f"No encontre productos que coincidan con '{consulta}' en el catalogo."

    lineas = []
    for p in productos:
        nombre = _texto_o_vacio(p.get("name"))
        marca = p.get("brand") or ""
        variantes = p.get("variants") or []
        precios = [float(v["price"]) for v in variantes if v.get("price")]
        hay_stock = any((v.get("stock") or 0) > 0 for v in variantes if v.get("stock_management"))
        rango_precio = f"${min(precios):,.0f}".replace(",", ".") if precios else "sin precio"
        if precios and max(precios) != min(precios):
            rango_precio += f" a ${max(precios):,.0f}".replace(",", ".")
        link = p.get("canonical_url") or ""
        lineas.append(
            f"- id={p['id']} | {nombre} ({marca}) | precio: {rango_precio} | "
            f"{'con stock' if hay_stock else 'sin stock'}"
            + (f" | link: {link}" if link else "")
        )

    return "\n".join(lineas)


async def obtener_detalle_producto(product_id: str) -> str:
    """
    Trae el detalle completo de UN producto: descripcion (con altura de taco/base y
    peso, si el producto los tiene cargados), y cada variante con talle, color, precio,
    precio promocional y stock exacto.

    Se llama despues de buscar_productos_tienda_nube, ya con el id del producto elegido.
    """
    if not TIENDANUBE_STORE_ID or not TIENDANUBE_ACCESS_TOKEN:
        return "No puedo consultar el catalogo ahora mismo: falta la conexion con Tienda Nube."

    url = f"{TIENDANUBE_BASE_URL}/{TIENDANUBE_STORE_ID}/products/{product_id}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as cliente:
            r = await cliente.get(url, headers=_headers())
    except httpx.HTTPError as e:
        logger.error(f"Error de red consultando el producto {product_id} en Tienda Nube: {e}")
        return "No pude conectarme al catalogo ahora mismo. Probemos de nuevo en un momento."

    if r.status_code == 404:
        return f"No encontre ningun producto con id {product_id}."
    if r.status_code != 200:
        logger.error(f"Tienda Nube rechazo el detalle del producto [{r.status_code}]: {r.text[:300]}")
        return "No pude consultar ese producto ahora mismo."

    p = r.json()
    nombre = _texto_o_vacio(p.get("name"))
    marca = p.get("brand") or ""
    descripcion = _texto_o_vacio(p.get("description"))
    url_publica = p.get("canonical_url", "")

    partes = [f"Producto: {nombre} ({marca})"]
    if descripcion:
        partes.append(f"Descripcion: {descripcion}")
    if url_publica:
        partes.append(f"Link: {url_publica}")

    partes.append("Variantes:")
    for v in p.get("variants", []):
        valores = ", ".join(_texto_o_vacio(val) for val in v.get("values", []))
        precio = float(v["price"]) if v.get("price") else None
        promo = float(v["promotional_price"]) if v.get("promotional_price") else None
        stock = v.get("stock")
        con_control_stock = v.get("stock_management", True)

        precio_txt = f"${precio:,.0f}".replace(",", ".") if precio is not None else "sin precio"
        if promo is not None and promo != precio:
            precio_txt += f" (promo: ${promo:,.0f})".replace(",", ".")

        if con_control_stock:
            stock_txt = f"{stock} unidades" if (stock or 0) > 0 else "SIN STOCK"
        else:
            stock_txt = "stock no controlado (consultar)"

        partes.append(f"  - {valores} | {precio_txt} | {stock_txt} | SKU: {v.get('sku', '-')}")

    return "\n".join(partes)


async def consultar_pedido(numero_pedido: str) -> str:
    """
    Busca un pedido por su numero (el que ve el cliente, no el id interno) y devuelve
    su estado de pago y de envio.
    """
    if not TIENDANUBE_STORE_ID or not TIENDANUBE_ACCESS_TOKEN:
        return "No puedo consultar pedidos ahora mismo: falta la conexion con Tienda Nube."

    url = f"{TIENDANUBE_BASE_URL}/{TIENDANUBE_STORE_ID}/orders"
    params = {"q": numero_pedido, "per_page": 10}

    try:
        async with httpx.AsyncClient(timeout=15.0) as cliente:
            r = await cliente.get(url, params=params, headers=_headers())
    except httpx.HTTPError as e:
        logger.error(f"Error de red consultando el pedido {numero_pedido} en Tienda Nube: {e}")
        return "No pude conectarme para consultar el pedido ahora mismo."

    if r.status_code == 404:
        # A diferencia de /products, cuando "q" no matchea ningun pedido Tiendanube
        # devuelve 404 ("Last page is 0") en vez de una lista vacia. No es un error:
        # simplemente no existe ese numero de pedido.
        return f"No encontre ningun pedido con el numero {numero_pedido}."

    if r.status_code != 200:
        logger.error(f"Tienda Nube rechazo la busqueda de pedidos [{r.status_code}]: {r.text[:300]}")
        return "No pude consultar el pedido ahora mismo."

    pedidos = r.json()
    # El filtro "q" busca por texto en varios campos; nos quedamos con el que matchea
    # exacto el numero de pedido para no confundir a un cliente con el pedido de otro.
    pedido = next((p for p in pedidos if str(p.get("number")) == str(numero_pedido)), None)
    if not pedido:
        return f"No encontre ningun pedido con el numero {numero_pedido}."

    estados_pago = {
        "authorized": "autorizado",
        "pending": "pendiente de pago",
        "paid": "pagado",
        "partially_paid": "pagado parcialmente",
        "abandoned": "abandonado",
        "refunded": "reembolsado",
        "partially_refunded": "reembolsado parcialmente",
        "voided": "anulado",
    }
    estados_envio = {
        "unpacked": "sin preparar",
        "shipped": "enviado",
        "unshipped": "sin enviar",
        "delivered": "entregado",
        "partially_packed": "parcialmente preparado",
        "partially_fulfilled": "parcialmente enviado",
    }

    pago = estados_pago.get(pedido.get("payment_status"), pedido.get("payment_status", "-"))
    envio = estados_envio.get(pedido.get("shipping_status"), pedido.get("shipping_status", "-"))
    total = pedido.get("total", "-")

    return (
        f"Pedido #{pedido.get('number')}: total ${total} | pago: {pago} | envio: {envio} | "
        f"estado general: {pedido.get('status', '-')}"
    )


def cargar_info_negocio() -> dict:
    """Carga la informacion del negocio desde config/business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


# ── Guia de talles ───────────────────────────────────────────────────────────
# Copia estatica de https://www.pampashop.com.ar/guia-de-talles/ (equivalencia de
# talles por marca, con el largo del pie en cm). No hay una API para esto -- es una
# pagina de contenido fijo, no un endpoint de Tienda Nube -- asi que se transcribe
# una vez aca en vez de scrapear la web en cada consulta. Si la tienda actualiza esa
# pagina, esta tabla hay que actualizarla a mano.
#
# Cada fila es (talla_bra, talla_arg, talla_cm) o (talla_arg, talla_cm) para las
# marcas que en la pagina no traen columna de Brasil.
GUIA_TALLES: dict[str, list[tuple]] = {
    "Barker": [(35, 22.5), (36, 23), (37, 24), (38, 24.5), (39, 25.5), (40, 26)],
    "Beira Rio": [
        (34, 35, 22.5), (35, 36, 23), (36, 37, 23.5), (37, 38, 24.5),
        (38, 39, 25), (39, 40, 26), (40, 41, 27),
    ],
    "Bestseller": [(36, 23), (37, 23.5), (38, 24.5), (39, 25.5), (40, 26)],
    "Bibi": [
        (14, 15, 9.5), (15, 16, 10.3), (16, 17, 11), (17, 18, 11.5), (18, 19, 12.3),
        (19, 20, 12.8), (20, 21, 13.3), (21, 22, 14), (22, 23, 14.5), (23, 24, 15),
        (24, 25, 15.5), (25, 26, 16), (26, 27, 17), (27, 28, 17.8), (28, 29, 18.5),
        (29, 30, 19), (30, 31, 20), (31, 32, 21), (32, 33, 21.5), (33, 34, 22),
        (34, 35, 22.5), (35, 36, 23.5), (36, 37, 24), (37, 38, 25),
    ],
    "Blue Duck": [
        (20, 13), (21, 14), (22, 15), (23, 15.5), (24, 16), (25, 17), (26, 17.5),
        (27, 18), (28, 18.5), (29, 19), (30, 20), (31, 20.5), (32, 21), (33, 21.5),
        (34, 22), (35, 22.5), (36, 23.5),
    ],
    "Br Sport": [
        (38, 39, 25.5), (39, 40, 26), (40, 41, 27), (41, 42, 27.5), (42, 43, 28),
        (43, 44, 29), (44, 45, 29.5),
    ],
    "Chocolate": [
        (34, 35, 21.5), (35, 36, 22.0), (36, 37, 23.0), (37, 38, 24.0),
        (38, 39, 25.0), (39, 40, 25.5), (40, 41, 26.5),
    ],
    "Dakota": [(36, 23.5), (37, 24), (38, 25), (39, 25.5), (40, 26)],
    "Dino Park": [
        (25, 15.5), (26, 16.4), (27, 17), (28, 17.5), (29, 18.5), (30, 19),
        (31, 19.5), (32, 20), (33, 21), (34, 21.5),
    ],
    "Fausto Milano": [(39, 25), (40, 26), (41, 27), (42, 28), (43, 29), (44, 30), (45, 31)],
    "Ferli (Zapatillas)": [
        (27, 18.5), (28, 19), (29, 19.5), (30, 20), (31, 21), (32, 21.5),
        (33, 22.5), (34, 23), (35, 23.5), (36, 24), (37, 25), (38, 25.5),
    ],
    "Freeway": [
        (40, 26), (41, 27), (42, 27.5), (43, 28.5), (44, 29), (45, 30),
        (46, 31), (47, 32), (48, 33),
    ],
    "Hopper (Zapato)": [
        (30, 19), (31, 20), (32, 20.5), (33, 21), (34, 21.5), (35, 22),
        (36, 23), (37, 23.5), (38, 24),
    ],
    "Ipanema Hombre": [(39, 25), (40, 26), (41, 26.5), (43, 27.5), (45, 29), (47, 30)],
    "Ipanema Mujer": [(35, 22.5), (36, 23), (37, 24), (38, 25), (39, 26), (40, 26.5)],
    "Karen Klier": [(36, 23.5), (37, 24), (38, 25), (39, 25.5), (40, 26)],
    "Kidy": [
        (18, 11.3), (19, 12), (20, 12.7), (21, 13.3), (22, 14), (23, 14.7),
        (24, 15.3), (25, 16), (26, 16.7), (27, 17.3), (28, 18), (29, 18.7),
        (30, 19.3), (31, 20), (32, 20.7), (33, 21.7), (34, 22), (35, 22.7),
        (36, 23.3), (37, 24),
    ],
    "Lady Stork": [(35, 23.5), (36, 24), (37, 24.5), (38, 25), (39, 25.5), (40, 26), (41, 27)],
    "Madero": [(35, 22.5), (36, 23), (37, 23.5), (38, 24), (39, 25), (40, 25.5)],
    "Marcel": [
        (20, 12), (21, 12.6), (22, 13.2), (23, 13.8), (24, 14.3), (27, 17.5),
        (28, 18), (29, 19), (30, 19.5), (31, 20.2), (32, 21), (33, 21.4),
        (34, 22.2), (35, 23), (36, 23.5), (37, 24.2), (38, 25),
    ],
    "Pegada Hombre": [
        (39, 25.5), (40, 26.5), (41, 27.5), (42, 28.5), (43, 29), (44, 29.5),
        (45, 30), (46, 31), (47, 32), (48, 32.5), (49, 33),
    ],
    "Modare Ultraconforto": [
        (34, 35, 22.5), (35, 36, 23), (36, 37, 23.5), (37, 38, 24.5),
        (38, 39, 25), (39, 40, 26), (40, 41, 27),
    ],
    "Moleca": [
        (34, 35, 22.5), (35, 36, 23), (36, 37, 23.5), (37, 38, 24.5),
        (38, 39, 25), (39, 40, 26), (40, 41, 27),
    ],
    "Molekinha": [
        (17, 18, 11.7), (18, 19, 12.5), (19, 20, 13), (20, 21, 13.8), (21, 22, 14.5),
        (22, 23, 14.8), (23, 24, 15.5), (24, 25, 16.3), (25, 26, 17), (26, 27, 18),
        (27, 28, 19), (28, 29, 19.5), (29, 30, 20), (30, 31, 20.6), (31, 32, 21.5),
        (32, 33, 22), (33, 34, 22.7), (34, 35, 23.02), (35, 36, 24), (36, 37, 25),
    ],
    "Molekinho": [
        (17, 18, 11.7), (18, 19, 12.5), (19, 20, 13), (20, 21, 13.8), (21, 22, 14.5),
        (22, 23, 14.9), (23, 24, 15.5), (24, 25, 16.3), (25, 26, 17), (26, 27, 18),
        (27, 28, 19), (28, 29, 19.5), (29, 30, 20), (30, 31, 20.6), (31, 32, 21.5),
        (32, 33, 22), (33, 34, 22.7), (34, 35, 23.02), (35, 36, 24), (36, 37, 25),
    ],
    "Morris": [(39, 26), (40, 26.5), (41, 27), (42, 28), (43, 28.5), (44, 29)],
    "Olympikus": [
        (36, 23.3), (37, 24), (38, 24.7), (39, 25.3), (40, 26), (41, 26.7),
        (42, 27.3), (43, 28), (44, 28.7), (45, 29.3),
    ],
    "Pegada Mujer": [(35, 22), (36, 22.5), (37, 23), (38, 24), (39, 24.5), (40, 25)],
    "Piccadilly": [
        (34, 35, 23), (35, 36, 23.5), (36, 37, 24), (37, 38, 25),
        (38, 39, 25.7), (39, 40, 26.7), (40, 41, 27.3),
    ],
    "Replay Hombre": [(39, 25), (40, 26), (41, 27), (42, 24.5), (43, 28), (44, 28.5), (45, 29)],
    "Replay Mujer": [(36, 23.5), (37, 24), (38, 24.5), (39, 25), (40, 26)],
    "Riot": [(35, 23.5), (36, 24), (37, 24.5), (38, 25), (39, 25.5), (40, 26.5)],
    "Savage": [(35, 23), (36, 23.5), (37, 24), (38, 24.5), (39, 25.5), (40, 26)],
    "Scarpino": [(39, 26), (40, 26.5), (41, 27.5), (42, 28.5), (43, 29), (44, 30)],
    "Soft": [
        (27, 17.5), (28, 18), (29, 19), (30, 19.5), (31, 20), (32, 20.5),
        (33, 21), (34, 22), (35, 22.5), (36, 23.5), (37, 24), (38, 24.5),
        (39, 25), (40, 25.5), (41, 26),
    ],
    "Sergio Tacchini Dama": [(35, 23.5), (36, 24), (37, 24.5), (38, 25), (39, 26), (40, 27)],
    "Ramarin": [(35, 23), (36, 24), (37, 24.5), (38, 25), (39, 25.5), (40, 26.5)],
    "Via Marte": [(35, 23), (36, 23.5), (37, 24), (38, 25), (39, 25.5), (40, 26)],
    "Vizzano": [
        (34, 35, 22.5), (35, 36, 23), (36, 37, 23.5), (37, 38, 24.5),
        (38, 39, 25), (39, 40, 26), (40, 41, 27),
    ],
    "Vizzia": [(35, 22.5), (36, 23.5), (37, 24), (38, 25), (39, 25.5), (40, 26.5)],
    "West Coast": [
        (39, 26), (40, 27), (41, 27.5), (42, 28.5), (43, 29.5), (44, 30.5), (45, 31),
    ],
    "Tres Corazones": [
        (35, 23), (36, 23.5), (37, 24), (38, 25), (39, 25.5), (40, 26), (41, 26.5),
    ],
    "Verenna": [(35, 22), (36, 22.4), (37, 23), (38, 23.4), (39, 24.3), (40, 24.7)],
    "Tookey": [
        (21, 14), (22, 14.5), (23, 15), (24, 15.5), (25, 16.5), (26, 17), (27, 18),
        (28, 18.5), (29, 19), (30, 20), (31, 20.5), (32, 21.5), (33, 22), (34, 22.5),
        (35, 23), (36, 24), (37, 24.5), (38, 24.7), (39, 25.3), (40, 26),
    ],
}


def _normalizar(texto: str) -> str:
    """Minusculas y sin acentos, para que 'Piccadilly' matchee con 'picadilly' o 'PICCADILLY'."""
    import unicodedata

    sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sin_acentos.lower().strip()


def _nombre_base(nombre_marca: str) -> str:
    """
    "Ipanema Hombre" y "Ipanema Mujer" -> "ipanema". Sirve para que un cliente que
    pregunta solo "ipanema" (sin aclarar genero) encuentre las dos variantes, y para
    armar la lista de marcas disponibles sin duplicados ni sufijos de genero.
    """
    import re

    base = re.sub(r"\(.*?\)", "", _normalizar(nombre_marca)).strip()
    for sufijo in (" hombre", " mujer", " dama"):
        if base.endswith(sufijo):
            base = base[: -len(sufijo)]
    return base.strip()


def _marcas_candidatas(consulta: str) -> list[str]:
    """Busca la marca pedida entre las claves de GUIA_TALLES, por nombre base o substring."""
    q = _normalizar(consulta)
    q_base = _nombre_base(consulta)

    exactas = [k for k in GUIA_TALLES if _nombre_base(k) == q_base]
    if exactas:
        return exactas

    return [k for k in GUIA_TALLES if q in _nombre_base(k) or _nombre_base(k) in q]


async def consultar_guia_talles(marca: str) -> str:
    """
    Tabla de equivalencia de talles de una marca puntual, con el largo del pie en
    centimetros para cada numero (y el talle de Brasil, cuando la marca lo usa).
    Datos fijos, transcriptos de la guia de talles publicada en la web de la tienda
    (no vienen de Tienda Nube: es contenido de una pagina, no de un producto).
    """
    candidatos = _marcas_candidatas(marca)
    if not candidatos:
        disponibles = sorted({_nombre_base(k).title() for k in GUIA_TALLES})
        return (
            f"No tengo guia de talles especifica para '{marca}'. "
            f"Marcas con guia disponible: {', '.join(disponibles)}."
        )

    partes = []
    for nombre in candidatos:
        lineas = []
        for fila in GUIA_TALLES[nombre]:
            if len(fila) == 3:
                bra, arg, cm = fila
                lineas.append(f"talle BRA {bra} / ARG {arg} = {cm} cm de pie")
            else:
                arg, cm = fila
                lineas.append(f"talle ARG {arg} = {cm} cm de pie")
        partes.append(f"Guia de talles {nombre}:\n" + "\n".join(lineas))

    return "\n\n".join(partes)
