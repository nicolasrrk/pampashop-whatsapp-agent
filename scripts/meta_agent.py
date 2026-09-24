# scripts/meta_agent.py — Configurar el Business Agent de Meta

"""
El Business Agent de Meta es el agente de IA propio de Meta, distinto de Pampa (que
corre en Railway). Se configura por su API, aparte de la Cloud API de mensajeria.

Este script arma la parte que necesita credenciales: el connector a Tienda Nube, que
es lo que le permite al agente de Meta consultar stock y precios reales en vez de
quedarse con lo que crawleo del sitio.

Los tokens se leen del .env y nunca se escriben en pantalla ni quedan en el historial
de la terminal.

Uso:
    python scripts/meta_agent.py estado          ver como esta configurado todo
    python scripts/meta_agent.py connector       crear el connector de Tienda Nube
    python scripts/meta_agent.py reautenticar    reintentar la credencial de un connector existente
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from dotenv import load_dotenv  # noqa: E402

# Se apunta al .env por ruta absoluta y no se confia en el directorio actual: el
# proyecto vive en una unidad de red, y una consola que no tenga mapeada la Z: (o que
# arranque en otro lado) se quedaria sin ninguna variable y fallaria sin decir por que.
load_dotenv(os.path.join(RAIZ, ".env"))

META_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
PHONE_ID = os.getenv("META_PHONE_NUMBER_ID", "")
TN_TOKEN = os.getenv("TIENDANUBE_ACCESS_TOKEN", "")
TN_STORE = os.getenv("TIENDANUBE_STORE_ID", "")
TN_VERSION = os.getenv("TIENDANUBE_API_VERSION") or "2025-03"

BASE = f"https://api.facebook.com/{PHONE_ID}"


def _llamar(ruta: str, metodo: str = "GET", cuerpo: dict | None = None, version: str = "2.0.0"):
    """Devuelve (status, datos). No levanta excepcion en los errores HTTP: los reporta."""
    req = urllib.request.Request(
        f"{BASE}/{ruta}",
        data=json.dumps(cuerpo, ensure_ascii=False).encode("utf-8") if cuerpo else None,
        method=metodo,
        headers={
            "Authorization": f"Bearer {META_TOKEN}",
            "X-API-Version": version,
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            texto = r.read().decode("utf-8")
            return r.status, (json.loads(texto) if texto else None)
    except urllib.error.HTTPError as e:
        texto = e.read().decode("utf-8")
        try:
            return e.code, json.loads(texto)
        except json.JSONDecodeError:
            return e.code, texto


def estado():
    """Muestra como quedo configurado el agente de Meta."""
    codigo, ajustes = _llamar("agent_config/settings")
    if codigo == 200 and ajustes:
        a = ajustes[0]
        prendido = a["rollout"]["enabled"]
        print(f"Rollout:            {'PRENDIDO — le contesta a los clientes' if prendido else 'apagado'}")
        print(f"Frases prohibidas:  {len(a['never_say_phrases'])}")
        print(f"Handoff:            {'si' if a['handoff']['enabled'] else 'no'}")
    else:
        print(f"Ajustes: HTTP {codigo} {ajustes}")

    codigo, sitios = _llamar("agent_config/websites")
    for s in sitios or []:
        print(f"Sitio:              {s['url']} — {s['crawl_status']}, "
              f"{s['pages_crawled']} paginas {s.get('crawl_error') or ''}")

    codigo, conectores = _llamar("agent_connectors")
    if not conectores:
        print("Connectors:         ninguno (correr: python scripts/meta_agent.py connector)")
    for c in conectores or []:
        print(f"Connector:          {c['name']} — {c['connection_status']['status']} "
              f"{c['connection_status'].get('error_message') or ''}")

    codigo, instrucciones = _llamar("agent_config/instructions", version="1.0.0")
    for i in instrucciones or []:
        print(f"Instruccion:        {i['title']} ({i['status']})")


def connector():
    """Crea el connector de Tienda Nube para que el agente consulte el catalogo real."""
    if not TN_TOKEN:
        print("Falta TIENDANUBE_ACCESS_TOKEN en el .env")
        return

    cuerpo = {
        # OJO: el campo "name" no acepta espacios ni guiones, aunque la documentacion
        # no lo diga. Confirmado a mano: "Tienda Nube PAMPA SHOP" y "tienda-nube" dan
        # los dos 400 "Invalid connector request"; "tiendanube" (una sola palabra) anda.
        "name": "tiendanubepampashop",
        "description": (
            f"Catalogo en vivo de la tienda online de PAMPA SHOP (Tienda Nube, tienda {TN_STORE}). "
            "Permite buscar calzado por nombre, marca o tipo con GET /products?q=, y obtener la "
            "ficha exacta de un producto con GET /products/{id}: talles disponibles con su stock "
            "real, colores, precio con descuentos vigentes, altura de taco, altura de plataforma, "
            "peso y link al producto. Tambien permite consultar el estado de un pedido con "
            "GET /orders?q=. Usar SIEMPRE esta fuente antes de afirmar talle, stock o precio: son "
            "datos que cambian todos los dias y el contenido crawleado del sitio puede estar viejo."
        ),
        "base_url": f"https://api.tiendanube.com/{TN_VERSION}/{TN_STORE}",
        # OJO: NO mandar "connector_protocol". La documentacion lo da como valido y
        # como default ("HTTP" si se omite), pero mandarlo explicito hace que la API
        # rechace el request entero con 400 "Invalid connector request". Confirmado
        # a mano: el mismo body sin este campo se crea bien.
        "auth_type": "API_KEY",
        "auth_config": {
            "api_key": {
                "headers": [
                    # Tienda Nube acepta el token como "Authorization: Bearer <token>".
                    {"field_name": "Authorization", "value": TN_TOKEN, "prefix": "Bearer "},
                    # Sin un User-Agent identificable, Tienda Nube responde 400.
                    {"field_name": "User-Agent",
                     "value": "PampaShop Meta Business Agent (ventas@pampashop.com.ar)"},
                ]
            }
        },
    }

    codigo, datos = _llamar("agent_connectors", metodo="POST", cuerpo=cuerpo)
    if codigo in (200, 201):
        print(f"Connector creado: {datos['id']}")
        print(f"Estado de conexion: {datos['connection_status']['status']}")
        if datos["connection_status"].get("error_message"):
            print(f"  {datos['connection_status']['error_message']}")
    elif codigo == 409:
        print("Ya existe un connector con ese nombre. Mira 'estado'.")
    else:
        print(f"HTTP {codigo}: {json.dumps(datos, ensure_ascii=False)[:500]}")


def connector_v2():
    """
    Igual que connector(), pero con el token y "Bearer " juntos en un solo campo
    "value", sin "prefix" separado. Existe porque upsertApiKey (para actualizar el
    connector original) da 500 y NO aplica el cambio; crear de cero si es confiable,
    asi que se prueba la hipotesis con un connector nuevo en vez de arriesgar el que
    ya funciona.
    """
    if not TN_TOKEN:
        print("Falta TIENDANUBE_ACCESS_TOKEN en el .env")
        return

    cuerpo = {
        "name": "tiendanubepampashopv2",
        "description": "Prueba: token y Bearer combinados en un solo campo, sin prefix.",
        "base_url": f"https://api.tiendanube.com/{TN_VERSION}/{TN_STORE}",
        "auth_type": "API_KEY",
        "auth_config": {
            "api_key": {
                "headers": [
                    {"field_name": "Authorization", "value": f"Bearer {TN_TOKEN}"},
                    {"field_name": "User-Agent",
                     "value": "PampaShop Meta Business Agent (ventas@pampashop.com.ar)"},
                ]
            }
        },
    }
    codigo, datos = _llamar("agent_connectors", metodo="POST", cuerpo=cuerpo)
    if codigo in (200, 201):
        print(f"Connector v2 creado: {datos['id']}")
    elif codigo == 500 and isinstance(datos, dict) and datos.get("detail") == "An unexpected error occurred":
        print("HTTP 500 generico: puede haberse creado igual. Correr 'estado' para confirmar.")
    else:
        print(f"HTTP {codigo}: {json.dumps(datos, ensure_ascii=False)[:400]}")


def reautenticar():
    """
    Reintenta la credencial del connector "tiendanubepampashop" con el token y el
    prefijo "Bearer " juntos en un solo campo "value", sin usar "prefix" por separado.

    Motivo: crear una herramienta sobre el connector real da siempre
    "Authorization failed", aunque el token funciona perfecto contra Tienda Nube
    llamado directo (verificado con agent/tools.py) y el connector muestra el prefix
    guardado bien en el GET. Sospecha: el validador que corre al crear una herramienta
    arma la llamada de prueba distinto a como lo hace en produccion, y no concatena
    "prefix" + "value" en ese paso. Juntarlo todo en "value" evita la duda.
    """
    if not TN_TOKEN:
        print("Falta TIENDANUBE_ACCESS_TOKEN en el .env")
        return

    codigo, conectores = _llamar("agent_connectors")
    objetivo = next((c for c in (conectores or []) if c["name"] == "tiendanubepampashop"), None)
    if not objetivo:
        print("No existe el connector 'tiendanubepampashop'. Corre primero 'connector'.")
        return

    cuerpo = {
        "api_key_config": {
            "headers": [
                {"field_name": "Authorization", "value": f"Bearer {TN_TOKEN}"},
                {"field_name": "User-Agent",
                 "value": "PampaShop Meta Business Agent (ventas@pampashop.com.ar)"},
            ]
        }
    }
    codigo, datos = _llamar(f"agent_connectors/{objetivo['id']}/upsertApiKey", metodo="POST", cuerpo=cuerpo)
    if codigo == 200:
        print(f"Credencial actualizada. Estado: {datos['connection_status']['status']}")
    else:
        print(f"HTTP {codigo}: {json.dumps(datos, ensure_ascii=False)[:400]}")


if __name__ == "__main__":
    if not META_TOKEN or not PHONE_ID:
        print("Faltan META_ACCESS_TOKEN o META_PHONE_NUMBER_ID en el .env")
        sys.exit(1)

    comando = sys.argv[1] if len(sys.argv) > 1 else "estado"
    if comando == "estado":
        estado()
    elif comando == "connector":
        connector()
    elif comando == "connector-v2":
        connector_v2()
    elif comando == "reautenticar":
        reautenticar()
    else:
        print(__doc__)
