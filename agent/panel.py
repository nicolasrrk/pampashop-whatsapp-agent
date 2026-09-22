# agent/panel.py — Panel web para mirar lo que contesta el agente

"""
Panel de solo lectura, servido por el mismo FastAPI del bot.

Se sirve desde aca y no desde una app aparte por una razon concreta: la base es
SQLite adentro del volumen de Railway, y ese archivo no lo puede abrir ningun otro
servicio. Un front separado tendria que pedirle los datos igual a este proceso, asi
que se ahorra el deploy, el CORS y el token entre servicios.

Muestra conversaciones, leads y escalados. No envia mensajes ni modifica nada.
"""

import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from agent.memory import listar_leads, obtener_conversacion_completa

logger = logging.getLogger("agentkit")

# Sin token no hay panel. Son conversaciones de clientes reales en una URL publica:
# si la variable no esta configurada, el panel devuelve 404 y no se expone nada. Es
# a proposito que falle cerrado y no que quede abierto "hasta que lo configuren".
PANEL_TOKEN = os.getenv("PANEL_TOKEN", "")

router = APIRouter(prefix="/panel", tags=["panel"])


def _verificar(request: Request) -> None:
    """
    El token puede venir por querystring (?token=...) o por cookie.

    La cookie existe para no arrastrar el token en la URL de cada llamada del panel
    una vez que entraste: la pagina lo guarda al cargar y las consultas siguientes
    viajan con ella.
    """
    if not PANEL_TOKEN:
        raise HTTPException(status_code=404, detail="Not Found")

    recibido = request.query_params.get("token") or request.cookies.get("panel_token") or ""
    if recibido != PANEL_TOKEN:
        raise HTTPException(status_code=401, detail="Token invalido")


@router.get("/datos/leads")
async def datos_leads(request: Request, limite: int = Query(50, ge=1, le=200)):
    """Lista de contactos, del que escribio mas recientemente al mas viejo."""
    _verificar(request)
    leads = await listar_leads(limite=limite)
    return [
        {
            "telefono": lead.telefono,
            "ultimo_mensaje": lead.ultimo_mensaje,
            "veces_contactado": lead.veces_contactado,
            "escalado": lead.escalado,
            "actualizado_en": lead.actualizado_en.isoformat() if lead.actualizado_en else None,
        }
        for lead in leads
    ]


@router.get("/datos/conversacion/{telefono}")
async def datos_conversacion(telefono: str, request: Request):
    """La conversacion completa con un cliente."""
    _verificar(request)
    return await obtener_conversacion_completa(telefono)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def panel(request: Request):
    """
    La pagina. Es un solo HTML con el JS adentro, sin build ni dependencias: el panel
    tiene que poder abrirse desde el celular sin instalar nada.
    """
    _verificar(request)
    token = request.query_params.get("token") or request.cookies.get("panel_token") or ""
    respuesta = HTMLResponse(PAGINA)
    # httponly=False a proposito: el JS de la pagina lo lee para las llamadas a /datos.
    respuesta.set_cookie("panel_token", token, max_age=60 * 60 * 24 * 30, samesite="strict")
    return respuesta


PAGINA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pampa — conversaciones</title>
<style>
  :root {
    --fondo: #ece5dd; --panel: #fff; --borde: #d9d2c9; --texto: #111b21;
    --suave: #667781; --cliente: #fff; --agente: #d9fdd3; --acento: #075e54;
    --alerta: #b42318; --alerta-fondo: #fef3f2;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --fondo: #0b141a; --panel: #111b21; --borde: #222d34; --texto: #e9edef;
      --suave: #8696a0; --cliente: #202c33; --agente: #005c4b; --acento: #00a884;
      --alerta: #ff8a80; --alerta-fondo: #2a1614;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--fondo); color: var(--texto);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  header {
    background: var(--acento); color: #fff; padding: 14px 16px;
    display: flex; align-items: center; gap: 12px; position: sticky; top: 0; z-index: 5;
  }
  header h1 { font-size: 17px; margin: 0; font-weight: 600; }
  header .contador { margin-left: auto; font-size: 13px; opacity: .85; }
  .volver {
    background: none; border: 0; color: #fff; font-size: 22px; cursor: pointer;
    padding: 0 4px; display: none; line-height: 1;
  }
  .envoltorio { max-width: 820px; margin: 0 auto; padding: 16px; }
  .chat {
    background: var(--panel); border: 1px solid var(--borde); border-radius: 10px;
    padding: 12px 14px; margin-bottom: 8px; cursor: pointer; display: flex; gap: 12px;
    align-items: baseline;
  }
  .chat:hover { border-color: var(--acento); }
  .chat .tel { font-weight: 600; white-space: nowrap; }
  .chat .ultimo {
    color: var(--suave); overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; flex: 1; min-width: 0; font-size: 14px;
  }
  .chat .cuando { color: var(--suave); font-size: 12px; white-space: nowrap; }
  .etiqueta {
    font-size: 11px; padding: 2px 7px; border-radius: 10px; font-weight: 600;
    background: var(--alerta-fondo); color: var(--alerta); white-space: nowrap;
  }
  .burbuja {
    max-width: 78%; padding: 8px 11px; border-radius: 8px; margin-bottom: 8px;
    white-space: pre-wrap; word-wrap: break-word; position: relative;
  }
  .de-cliente { background: var(--cliente); border: 1px solid var(--borde); margin-right: auto; }
  .de-agente { background: var(--agente); margin-left: auto; }
  .hora { font-size: 11px; color: var(--suave); display: block; margin-top: 3px; }
  .vacio { text-align: center; color: var(--suave); padding: 48px 16px; }
  .error { background: var(--alerta-fondo); color: var(--alerta); padding: 12px; border-radius: 8px; }
</style>
</head>
<body>
<header>
  <button class="volver" id="volver" aria-label="Volver">&larr;</button>
  <h1 id="titulo">Conversaciones</h1>
  <span class="contador" id="contador"></span>
</header>
<div class="envoltorio" id="contenido"><p class="vacio">Cargando…</p></div>

<script>
const contenido = document.getElementById("contenido");
const titulo = document.getElementById("titulo");
const contador = document.getElementById("contador");
const volver = document.getElementById("volver");
let refresco = null;

const escapar = (t) => { const d = document.createElement("div"); d.textContent = t ?? ""; return d.innerHTML; };

function fecha(iso) {
  if (!iso) return "";
  const d = new Date(iso), hoy = new Date();
  const hora = d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === hoy.toDateString()) return hora;
  return d.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit" }) + " " + hora;
}

async function pedir(ruta) {
  const r = await fetch(ruta, { credentials: "same-origin" });
  if (!r.ok) throw new Error(r.status === 401 ? "Token invalido" : "Error " + r.status);
  return r.json();
}

async function verLista() {
  volver.style.display = "none";
  titulo.textContent = "Conversaciones";
  try {
    const leads = await pedir("/panel/datos/leads");
    const escalados = leads.filter(l => l.escalado).length;
    contador.textContent = leads.length + " chats" + (escalados ? " · " + escalados + " esperando" : "");
    if (!leads.length) { contenido.innerHTML = '<p class="vacio">Todavia no escribio nadie.</p>'; return; }
    // Los que pidieron una persona van arriba: son los que estan esperando.
    leads.sort((a, b) => (b.escalado === true) - (a.escalado === true));
    contenido.innerHTML = leads.map(l => `
      <div class="chat" onclick="verChat('${escapar(l.telefono)}')">
        <span class="tel">${escapar(l.telefono)}</span>
        ${l.escalado ? '<span class="etiqueta">espera persona</span>' : ""}
        <span class="ultimo">${escapar(l.ultimo_mensaje)}</span>
        <span class="cuando">${fecha(l.actualizado_en)}</span>
      </div>`).join("");
  } catch (e) {
    contenido.innerHTML = '<p class="error">' + escapar(e.message) + "</p>";
  }
}

async function verChat(telefono) {
  clearInterval(refresco);
  volver.style.display = "block";
  titulo.textContent = telefono;
  try {
    const msgs = await pedir("/panel/datos/conversacion/" + encodeURIComponent(telefono));
    contador.textContent = msgs.length + " mensajes";
    contenido.innerHTML = msgs.length
      ? msgs.map(m => `
          <div class="burbuja ${m.role === "user" ? "de-cliente" : "de-agente"}">
            ${escapar(m.content)}<span class="hora">${fecha(m.timestamp)}</span>
          </div>`).join("")
      : '<p class="vacio">Sin mensajes guardados.</p>';
    window.scrollTo(0, document.body.scrollHeight);
  } catch (e) {
    contenido.innerHTML = '<p class="error">' + escapar(e.message) + "</p>";
  }
}

volver.onclick = () => { verLista(); refresco = setInterval(verLista, 30000); };
verLista();
refresco = setInterval(verLista, 30000);  // la lista se refresca sola; un chat abierto no
</script>
</body>
</html>
"""
