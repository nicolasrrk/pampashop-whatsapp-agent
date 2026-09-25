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
