# agent/panel.py — Panel web para ver y aprobar lo que contesta el agente

"""
Panel servido por el mismo FastAPI del bot.

Se sirve desde aca y no desde una app aparte por una razon concreta: la base es
SQLite adentro del volumen de Railway, y ese archivo no lo puede abrir ningun otro
servicio. Un front separado tendria que pedirle los datos igual a este proceso, asi
que se ahorra el deploy, el CORS y el token entre servicios.

Muestra conversaciones, leads y escalados, y permite aprobar o descartar los
borradores cuando MODO_ENVIO=borrador. Aprobar SI envia un WhatsApp real: es la misma
operacion que scripts/bandeja.py, con los mismos pasos y en el mismo orden.
"""

import json
import logging
import os

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from agent.memory import (
    guardar_mensaje,
    listar_borradores_pendientes,
    listar_leads,
    marcar_borrador,
    obtener_conversacion_completa,
)

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


# ── Datos ────────────────────────────────────────────────────────────────────


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


@router.get("/datos/borradores")
async def datos_borradores(request: Request):
    """Las respuestas redactadas que estan esperando aprobacion."""
    _verificar(request)
    borradores = await listar_borradores_pendientes()
    return [
        {
            "id": b.id,
            "telefono": b.telefono,
            "mensaje_cliente": b.mensaje_cliente,
            "respuesta": b.respuesta,
            "creado_en": b.creado_en.isoformat() if b.creado_en else None,
        }
        for b in borradores
    ]


# ── Acciones ─────────────────────────────────────────────────────────────────


@router.post("/accion/borrador/{borrador_id}")
async def accion_borrador(
    borrador_id: int,
    request: Request,
    cuerpo: dict = Body(default={}),
):
    """
    Aprueba (y envia) o descarta un borrador.

    Es la misma secuencia que scripts/bandeja.py, y el orden importa: primero se
    intenta enviar y recien si Meta lo acepta se marca como aprobado y se guarda en
    el historial. Si se marcara antes, un envio fallido dejaria el borrador cerrado
    sin que al cliente le haya llegado nada, y nadie se enteraria.
    """
    _verificar(request)

    accion = (cuerpo.get("accion") or "").strip().lower()
    if accion not in ("aprobar", "descartar"):
        raise HTTPException(status_code=400, detail="Accion invalida")

    pendientes = {b.id: b for b in await listar_borradores_pendientes()}
    borrador = pendientes.get(borrador_id)
    if borrador is None:
        # Ya lo resolvio otro (la Console, otra pestana del panel) o no existe.
        raise HTTPException(status_code=404, detail="Ese borrador ya no esta pendiente")

    if accion == "descartar":
        await marcar_borrador(borrador_id, "descartado")
        logger.info(f"Borrador {borrador_id} descartado desde el panel")
        return {"ok": True, "estado": "descartado"}

    # El texto se puede editar antes de enviar; si no viene nada, va el del agente.
    texto = (cuerpo.get("texto") or "").strip() or borrador.respuesta

    # Import local: el proveedor se resuelve al usarlo, no al importar el modulo, para
    # que un .env incompleto no impida que el panel abra y muestre el diagnostico.
    from agent.providers import obtener_proveedor

    contexto = json.loads(borrador.contexto_json or "{}")
    enviado = await obtener_proveedor().enviar_mensaje(borrador.telefono, texto, contexto)

    if not enviado:
        logger.error(f"Borrador {borrador_id}: no se pudo enviar, queda pendiente")
        raise HTTPException(status_code=502, detail="No se pudo enviar. Queda pendiente.")

    await marcar_borrador(borrador_id, "aprobado")
    await guardar_mensaje(borrador.telefono, "user", borrador.mensaje_cliente)
    await guardar_mensaje(borrador.telefono, "assistant", texto)
    logger.info(f"Borrador {borrador_id} aprobado y enviado desde el panel")
    return {"ok": True, "estado": "enviado"}


# ── Pagina ───────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def panel(request: Request):
    """Un solo HTML con el JS adentro: tiene que abrirse en el celular sin instalar nada."""
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
<title>Pampa</title>
<style>
  :root {
    --fondo:#f0f2f5; --panel:#fff; --borde:#e4e6eb; --texto:#111b21; --suave:#667781;
    --cliente:#fff; --agente:#d9fdd3; --acento:#128c7e; --acento2:#25d366;
    --alerta:#b42318; --alerta-bg:#fef3f2; --ambar:#8a5a00; --ambar-bg:#fff8e6;
    --sombra:0 1px 2px rgba(0,0,0,.08);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --fondo:#0b141a; --panel:#111b21; --borde:#222d34; --texto:#e9edef; --suave:#8696a0;
      --cliente:#202c33; --agente:#005c4b; --acento:#00a884; --acento2:#00a884;
      --alerta:#ff8a80; --alerta-bg:#2a1614; --ambar:#ffc94d; --ambar-bg:#2a2314;
      --sombra:0 1px 2px rgba(0,0,0,.3);
    }
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body {
    margin:0; background:var(--fondo); color:var(--texto);
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    padding-bottom:env(safe-area-inset-bottom);
  }
  header {
    background:var(--acento); color:#fff; position:sticky; top:0; z-index:10;
    box-shadow:0 1px 3px rgba(0,0,0,.2);
  }
  .barra { display:flex; align-items:center; gap:10px; padding:13px 16px; }
  .barra h1 { font-size:17px; margin:0; font-weight:600; flex:1;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .vivo { font-size:12px; opacity:.9; display:flex; align-items:center; gap:5px; }
  .punto { width:7px; height:7px; border-radius:50%; background:#8bf; box-shadow:0 0 0 0 rgba(140,255,180,.7); }
  .punto.on { background:#b9f6ca; animation:latido 2s infinite; }
  @keyframes latido { 0%{box-shadow:0 0 0 0 rgba(185,246,202,.7)} 70%{box-shadow:0 0 0 7px rgba(185,246,202,0)} 100%{box-shadow:0 0 0 0 rgba(185,246,202,0)} }
  .volver { background:none; border:0; color:#fff; font-size:23px; cursor:pointer; padding:0 2px; line-height:1; display:none; }
  nav { display:flex; }
  nav button {
    flex:1; background:none; border:0; border-bottom:3px solid transparent; color:#fff;
    opacity:.7; padding:11px 8px; font-size:13.5px; font-weight:600; cursor:pointer;
    letter-spacing:.3px; font-family:inherit;
  }
  nav button.activa { opacity:1; border-bottom-color:#fff; }
  nav .globo {
    background:#ff5252; color:#fff; border-radius:9px; padding:1px 6px;
    font-size:11px; margin-left:5px; display:inline-block;
  }
  main { max-width:860px; margin:0 auto; padding:14px 16px 28px; }

  .chat {
    background:var(--panel); border-radius:12px; padding:13px 15px; margin-bottom:9px;
    cursor:pointer; box-shadow:var(--sombra); display:flex; gap:11px; align-items:center;
    border-left:3px solid transparent; transition:transform .06s;
  }
  .chat:active { transform:scale(.995); }
  .chat.espera { border-left-color:var(--alerta); }
  .avatar {
    width:40px; height:40px; border-radius:50%; background:var(--acento);
    color:#fff; display:grid; place-items:center; font-weight:600; font-size:14px; flex-shrink:0;
  }
  .medio { flex:1; min-width:0; }
  .tel { font-weight:600; font-size:14.5px; }
  .ultimo { color:var(--suave); font-size:13.5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .cuando { color:var(--suave); font-size:11.5px; white-space:nowrap; align-self:flex-start; }
  .etiqueta { font-size:10.5px; padding:2px 7px; border-radius:10px; font-weight:700;
    background:var(--alerta-bg); color:var(--alerta); text-transform:uppercase; letter-spacing:.4px; }

  .burbuja { max-width:80%; padding:8px 11px; border-radius:9px; margin-bottom:9px;
    white-space:pre-wrap; word-wrap:break-word; box-shadow:var(--sombra); }
  .de-cliente { background:var(--cliente); margin-right:auto; border-top-left-radius:2px; }
  .de-agente { background:var(--agente); margin-left:auto; border-top-right-radius:2px; }
  .hora { font-size:10.5px; color:var(--suave); display:block; margin-top:3px; text-align:right; }
  .nuevo { animation:entra .3s ease-out; }
  @keyframes entra { from{opacity:0; transform:translateY(6px)} to{opacity:1; transform:none} }

  .tarjeta { background:var(--panel); border-radius:12px; padding:15px; margin-bottom:12px; box-shadow:var(--sombra); }
  .tarjeta .de { font-size:12px; color:var(--suave); margin-bottom:9px; }
  .dijo { background:var(--fondo); border-left:3px solid var(--suave); padding:9px 11px;
    border-radius:0 8px 8px 0; margin-bottom:11px; font-size:14px; }
  .dijo b { display:block; font-size:11px; color:var(--suave); text-transform:uppercase;
    letter-spacing:.4px; margin-bottom:3px; font-weight:700; }
  textarea {
    width:100%; border:1px solid var(--borde); border-radius:9px; padding:10px 11px;
    font:inherit; font-size:14px; background:var(--agente); color:var(--texto);
    resize:vertical; min-height:96px;
  }
  .acciones { display:flex; gap:9px; margin-top:11px; }
  .acciones button { flex:1; border:0; border-radius:9px; padding:11px; font-size:14.5px;
    font-weight:600; cursor:pointer; font-family:inherit; }
  .enviar { background:var(--acento2); color:#fff; }
  .tirar { background:var(--fondo); color:var(--suave); border:1px solid var(--borde) !important; }
  .acciones button:disabled { opacity:.5; cursor:default; }

  .vacio { text-align:center; color:var(--suave); padding:56px 16px; }
  .vacio .icono { font-size:40px; display:block; margin-bottom:10px; opacity:.5; }
  .error { background:var(--alerta-bg); color:var(--alerta); padding:12px 14px; border-radius:9px; }
  .aviso { position:fixed; left:50%; bottom:22px; transform:translateX(-50%);
    background:var(--texto); color:var(--fondo); padding:10px 18px; border-radius:22px;
    font-size:14px; box-shadow:0 3px 14px rgba(0,0,0,.3); z-index:50; }
</style>
</head>
<body>
<header>
  <div class="barra">
    <button class="volver" id="volver" aria-label="Volver">&larr;</button>
    <h1 id="titulo">Pampa</h1>
    <span class="vivo"><span class="punto" id="punto"></span><span id="contador"></span></span>
  </div>
  <nav id="nav">
    <button data-vista="chats" class="activa">CHATS</button>
    <button data-vista="borradores">POR APROBAR<span class="globo" id="globo" style="display:none">0</span></button>
  </nav>
</header>
<main id="contenido"><p class="vacio">Cargando…</p></main>

<script>
const $ = (id) => document.getElementById(id);
const contenido = $("contenido"), titulo = $("titulo"), contador = $("contador");
const volver = $("volver"), punto = $("punto"), globo = $("globo"), nav = $("nav");

let vista = "chats";        // chats | borradores | conversacion
let telActual = null;
let ultimaFirma = "";       // para no repintar (y no perder el scroll) si nada cambio

const escapar = (t) => { const d = document.createElement("div"); d.textContent = t ?? ""; return d.innerHTML; };
const iniciales = (tel) => String(tel).slice(-2);

function fecha(iso) {
  if (!iso) return "";
  const d = new Date(iso), hoy = new Date();
  const hora = d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === hoy.toDateString()) return hora;
  const ayer = new Date(hoy); ayer.setDate(hoy.getDate() - 1);
  if (d.toDateString() === ayer.toDateString()) return "ayer " + hora;
  return d.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit" }) + " " + hora;
}

function aviso(texto) {
  const d = document.createElement("div");
  d.className = "aviso"; d.textContent = texto;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 2600);
}

async function pedir(ruta, opciones) {
  const r = await fetch(ruta, Object.assign({ credentials: "same-origin" }, opciones || {}));
  if (!r.ok) {
    let detalle = "Error " + r.status;
    if (r.status === 401) detalle = "Token invalido";
    else { try { detalle = (await r.json()).detail || detalle; } catch (e) {} }
    throw new Error(detalle);
  }
  return r.json();
}

// ── Pintado ────────────────────────────────────────────────────────────────

function pintarChats(leads) {
  contador.textContent = leads.length + (leads.length === 1 ? " chat" : " chats");
  if (!leads.length) {
    contenido.innerHTML = '<p class="vacio"><span class="icono">&#128172;</span>Todavia no escribio nadie.</p>';
    return;
  }
  // Los que pidieron una persona van arriba: son los que estan esperando.
  const orden = leads.slice().sort((a, b) => (b.escalado === true) - (a.escalado === true));
  contenido.innerHTML = orden.map(l => `
    <div class="chat ${l.escalado ? "espera" : ""}" onclick="abrirChat('${escapar(l.telefono)}')">
      <div class="avatar">${escapar(iniciales(l.telefono))}</div>
      <div class="medio">
        <div class="tel">${escapar(l.telefono)} ${l.escalado ? '<span class="etiqueta">espera persona</span>' : ""}</div>
        <div class="ultimo">${escapar(l.ultimo_mensaje)}</div>
      </div>
      <span class="cuando">${fecha(l.actualizado_en)}</span>
    </div>`).join("");
}

function pintarConversacion(msgs, alFinal) {
  contador.textContent = msgs.length + " mensajes";
  contenido.innerHTML = msgs.length
    ? msgs.map((m, i) => `
        <div class="burbuja ${m.role === "user" ? "de-cliente" : "de-agente"} ${alFinal && i >= msgs.length - 1 ? "nuevo" : ""}">
          ${escapar(m.content)}<span class="hora">${fecha(m.timestamp)}</span>
        </div>`).join("")
    : '<p class="vacio">Sin mensajes guardados.</p>';
}

function pintarBorradores(bs) {
  contador.textContent = bs.length ? bs.length + " esperando" : "al dia";
  if (!bs.length) {
    contenido.innerHTML = '<p class="vacio"><span class="icono">&#9989;</span>No hay nada para aprobar.</p>';
    return;
  }
  contenido.innerHTML = bs.map(b => `
    <div class="tarjeta" id="b${b.id}">
      <div class="de"><b>${escapar(b.telefono)}</b> &middot; ${fecha(b.creado_en)}</div>
      <div class="dijo"><b>El cliente escribio</b>${escapar(b.mensaje_cliente)}</div>
      <textarea id="t${b.id}">${escapar(b.respuesta)}</textarea>
      <div class="acciones">
        <button class="enviar" onclick="resolver(${b.id},'aprobar')">Enviar</button>
        <button class="tirar" onclick="resolver(${b.id},'descartar')">Descartar</button>
      </div>
    </div>`).join("");
}

// ── Acciones ───────────────────────────────────────────────────────────────

async function resolver(id, accion) {
  const tarjeta = $("b" + id);
  const botones = tarjeta.querySelectorAll("button");
  if (accion === "aprobar" && !confirm("Se le envia este mensaje al cliente por WhatsApp. ¿Confirmas?")) return;
  botones.forEach(b => b.disabled = true);
  try {
    const r = await pedir("/panel/accion/borrador/" + id, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accion, texto: $("t" + id).value }),
    });
    tarjeta.remove();
    aviso(r.estado === "enviado" ? "Enviado al cliente" : "Descartado");
    ultimaFirma = "";
    refrescar();
  } catch (e) {
    botones.forEach(b => b.disabled = false);
    aviso(e.message);
  }
}

function abrirChat(telefono) {
  vista = "conversacion"; telActual = telefono; ultimaFirma = "";
  volver.style.display = "block";
  nav.style.display = "none";
  titulo.textContent = telefono;
  refrescar(true);
}

volver.onclick = () => {
  vista = "chats"; telActual = null; ultimaFirma = "";
  volver.style.display = "none";
  nav.style.display = "flex";
  titulo.textContent = "Pampa";
  refrescar();
};

nav.onclick = (e) => {
  const boton = e.target.closest("button[data-vista]");
  if (!boton) return;
  nav.querySelectorAll("button").forEach(b => b.classList.remove("activa"));
  boton.classList.add("activa");
  vista = boton.dataset.vista; ultimaFirma = "";
  refrescar();
};

// ── Refresco en vivo ───────────────────────────────────────────────────────
// Cada 4 segundos. Se repinta SOLO si los datos cambiaron (se compara una firma):
// repintar siempre reiniciaria el scroll del chat y borraria lo que estes editando
// en un borrador. Si la pestana no esta a la vista, no se pide nada.

async function refrescar(forzarAbajo) {
  if (document.hidden) return;
  punto.classList.add("on");
  try {
    if (vista === "conversacion") {
      const msgs = await pedir("/panel/datos/conversacion/" + encodeURIComponent(telActual));
      const firma = JSON.stringify(msgs.map(m => m.timestamp + m.content.length));
      if (firma !== ultimaFirma) {
        const abajo = forzarAbajo || (window.innerHeight + window.scrollY >= document.body.scrollHeight - 120);
        pintarConversacion(msgs, ultimaFirma !== "");
        ultimaFirma = firma;
        if (abajo) window.scrollTo(0, document.body.scrollHeight);
      }
    } else if (vista === "borradores") {
      const bs = await pedir("/panel/datos/borradores");
      globo.textContent = bs.length; globo.style.display = bs.length ? "inline-block" : "none";
      const firma = JSON.stringify(bs.map(b => b.id));
      if (firma !== ultimaFirma) { pintarBorradores(bs); ultimaFirma = firma; }
    } else {
      const [leads, bs] = await Promise.all([
        pedir("/panel/datos/leads"),
        pedir("/panel/datos/borradores").catch(() => []),
      ]);
      globo.textContent = bs.length; globo.style.display = bs.length ? "inline-block" : "none";
      const firma = JSON.stringify(leads.map(l => l.telefono + l.actualizado_en + l.escalado));
      if (firma !== ultimaFirma) { pintarChats(leads); ultimaFirma = firma; }
    }
  } catch (e) {
    contenido.innerHTML = '<p class="error">' + escapar(e.message) + "</p>";
  } finally {
    setTimeout(() => punto.classList.remove("on"), 600);
  }
}

setInterval(refrescar, 4000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refrescar(); });
refrescar();
</script>
</body>
</html>
"""
