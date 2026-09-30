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
    alternar_bot,
    guardar_mensaje,
    listar_borradores_pendientes,
    listar_leads,
    marcar_borrador,
    obtener_conversacion_completa,
    obtener_metricas,
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
            "bot_activo": lead.bot_activo,
            "actualizado_en": lead.actualizado_en.isoformat() if lead.actualizado_en else None,
        }
        for lead in leads
    ]


@router.get("/datos/conversacion/{telefono}")
async def datos_conversacion(telefono: str, request: Request):
    """La conversacion completa con un cliente."""
    _verificar(request)
    return await obtener_conversacion_completa(telefono)


@router.get("/datos/metricas")
async def datos_metricas(request: Request):
    """Todo lo que pinta el dashboard: mensajes, escalados, leads y consumo de Claude."""
    _verificar(request)
    return await obtener_metricas()


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


@router.post("/accion/bot/{telefono}")
async def accion_bot(telefono: str, request: Request, cuerpo: dict = Body(default={})):
    """
    Prende o apaga a Fran para UN numero puntual (toggle "bot activo" del panel).

    Con el bot apagado, main.py deja de generarle respuestas automaticas a ese numero
    -- el mensaje del cliente se sigue guardando y viendo en el panel, pero contestarlo
    pasa a ser trabajo de una persona, con /accion/responder.
    """
    _verificar(request)
    activo = cuerpo.get("activo")
    if not isinstance(activo, bool):
        raise HTTPException(status_code=400, detail="Falta 'activo' (true/false)")

    ok = await alternar_bot(telefono, activo)
    if not ok:
        raise HTTPException(status_code=404, detail="Ese telefono no tiene conversacion registrada")

    logger.info(f"{telefono}: bot {'activado' if activo else 'desactivado'} desde el panel")
    return {"ok": True, "bot_activo": activo}


@router.post("/accion/responder/{telefono}")
async def accion_responder(telefono: str, request: Request, cuerpo: dict = Body(default={})):
    """
    Manda un mensaje escrito a mano por una persona del equipo, directo por WhatsApp.

    A diferencia de un borrador (que es la respuesta que REDACTO Fran, pendiente de
    aprobacion), esto es texto que tipeo una persona: sale apenas se confirma, sin pasar
    por MODO_ENVIO=borrador -- ya es, en si mismo, la aprobacion humana.
    """
    _verificar(request)
    texto = (cuerpo.get("texto") or "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail="Falta 'texto'")

    from agent.providers import obtener_proveedor

    enviado = await obtener_proveedor().enviar_mensaje(telefono, texto)
    if not enviado:
        raise HTTPException(status_code=502, detail="No se pudo enviar el mensaje")

    await guardar_mensaje(telefono, "assistant", texto, remitente="humano")
    logger.info(f"Mensaje manual enviado a {telefono} desde el panel")
    return {"ok": True}


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


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Pagina de metricas, separada del panel de chats: otra estetica, otro proposito."""
    _verificar(request)
    token = request.query_params.get("token") or request.cookies.get("panel_token") or ""
    respuesta = HTMLResponse(DASHBOARD_PAGINA)
    respuesta.set_cookie("panel_token", token, max_age=60 * 60 * 24 * 30, samesite="strict")
    return respuesta


PAGINA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fran</title>
<style>
  :root {
    --fondo:#f0f2f5; --panel:#fff; --borde:#e4e6eb; --texto:#111b21; --suave:#667781;
    --cliente:#fff; --agente:#d9fdd3; --humano:#cfe4ff; --acento:#128c7e; --acento2:#25d366;
    --alerta:#b42318; --alerta-bg:#fef3f2; --ambar:#8a5a00; --ambar-bg:#fff8e6;
    --sombra:0 1px 2px rgba(0,0,0,.08);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --fondo:#0b141a; --panel:#111b21; --borde:#222d34; --texto:#e9edef; --suave:#8696a0;
      --cliente:#202c33; --agente:#005c4b; --humano:#1f3a5c; --acento:#00a884; --acento2:#00a884;
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
  .etiqueta.manual { background:var(--ambar-bg); color:var(--ambar); margin-left:4px; }

  .burbuja { max-width:80%; padding:8px 11px; border-radius:9px; margin-bottom:9px;
    white-space:pre-wrap; word-wrap:break-word; box-shadow:var(--sombra); }
  .de-cliente { background:var(--cliente); margin-right:auto; border-top-left-radius:2px; }
  .de-agente { background:var(--agente); margin-left:auto; border-top-right-radius:2px; }
  .de-humano { background:var(--humano); margin-left:auto; border-top-right-radius:2px; }
  .remitente-label { display:block; font-size:10px; font-weight:700; text-transform:uppercase;
    letter-spacing:.4px; opacity:.65; margin-bottom:3px; }
  .hora { font-size:10.5px; color:var(--suave); display:block; margin-top:3px; text-align:right; }
  .nuevo { animation:entra .3s ease-out; }
  @keyframes entra { from{opacity:0; transform:translateY(6px)} to{opacity:1; transform:none} }

  .controlbot { display:none; align-items:center; gap:9px; padding:8px 16px;
    background:rgba(0,0,0,.12); font-size:13px; color:#fff; }
  .switch { position:relative; display:inline-block; width:38px; height:22px; flex-shrink:0; }
  .switch input { opacity:0; width:0; height:0; }
  .slider { position:absolute; cursor:pointer; inset:0; background:rgba(255,255,255,.35);
    transition:.2s; border-radius:22px; }
  .slider:before { content:""; position:absolute; height:16px; width:16px; left:3px; bottom:3px;
    background:#fff; transition:.2s; border-radius:50%; }
  .switch input:checked + .slider { background:#fff; }
  .switch input:checked + .slider:before { transform:translateX(16px); background:var(--acento); }

  body.con-caja { padding-bottom:78px; }
  .caja-responder { display:none; position:fixed; left:0; right:0; bottom:0; z-index:20;
    background:var(--panel); border-top:1px solid var(--borde); gap:8px; align-items:flex-end;
    padding:10px 12px calc(10px + env(safe-area-inset-bottom)); }
  .caja-responder textarea { flex:1; border:1px solid var(--borde); border-radius:18px;
    padding:9px 14px; font:inherit; font-size:14px; resize:none; min-height:20px; max-height:100px;
    background:var(--fondo); color:var(--texto); }
  .caja-responder button { background:var(--acento2); color:#fff; border:0; border-radius:18px;
    padding:9px 18px; font-weight:600; cursor:pointer; font-family:inherit; flex-shrink:0; }
  .caja-responder button:disabled { opacity:.5; cursor:default; }

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
    <h1 id="titulo">Fran</h1>
    <a id="linkMetricas" href="#" style="color:#fff;opacity:.85;text-decoration:none;font-size:19px;line-height:1;margin-right:2px;" title="Metricas">&#128202;</a>
    <span class="vivo"><span class="punto" id="punto"></span><span id="contador"></span></span>
  </div>
  <nav id="nav">
    <button data-vista="chats" class="activa">CHATS</button>
    <button data-vista="borradores">POR APROBAR<span class="globo" id="globo" style="display:none">0</span></button>
  </nav>
  <div class="controlbot" id="controlBot">
    <label class="switch">
      <input type="checkbox" id="switchBot">
      <span class="slider"></span>
    </label>
    <span id="estadoBot">Bot activo</span>
  </div>
</header>
<main id="contenido"><p class="vacio">Cargando…</p></main>
<div class="caja-responder" id="cajaResponder">
  <textarea id="textoResponder" placeholder="Escribir como humano…" rows="1"></textarea>
  <button id="btnResponder" onclick="enviarManual()">Enviar</button>
</div>

<script>
const $ = (id) => document.getElementById(id);
const contenido = $("contenido"), titulo = $("titulo"), contador = $("contador");
const volver = $("volver"), punto = $("punto"), globo = $("globo"), nav = $("nav");
const controlBot = $("controlBot"), switchBot = $("switchBot"), estadoBot = $("estadoBot");
const cajaResponder = $("cajaResponder"), textoResponder = $("textoResponder"), btnResponder = $("btnResponder");

let vista = "chats";        // chats | borradores | conversacion
let telActual = null;
let ultimaFirma = "";       // para no repintar (y no perder el scroll) si nada cambio
let leadsPorTelefono = {};  // cache de la ultima lista de leads, para leer bot_activo al abrir un chat

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
  leadsPorTelefono = {};
  leads.forEach(l => { leadsPorTelefono[l.telefono] = l; });

  contador.textContent = leads.length + (leads.length === 1 ? " chat" : " chats");
  if (!leads.length) {
    contenido.innerHTML = '<p class="vacio"><span class="icono">&#128172;</span>Todavia no escribio nadie.</p>';
    return;
  }
  // Los que necesitan atencion (esperan una persona, o ya la tienen atendiendo a
  // mano) van arriba.
  const necesitaAtencion = (l) => l.escalado || !l.bot_activo;
  const orden = leads.slice().sort((a, b) => necesitaAtencion(b) - necesitaAtencion(a));
  contenido.innerHTML = orden.map(l => `
    <div class="chat ${necesitaAtencion(l) ? "espera" : ""}" onclick="abrirChat('${escapar(l.telefono)}')">
      <div class="avatar">${escapar(iniciales(l.telefono))}</div>
      <div class="medio">
        <div class="tel">${escapar(l.telefono)}
          ${l.escalado ? '<span class="etiqueta">espera persona</span>' : ""}
          ${!l.bot_activo ? '<span class="etiqueta manual">modo manual</span>' : ""}
        </div>
        <div class="ultimo">${escapar(l.ultimo_mensaje)}</div>
      </div>
      <span class="cuando">${fecha(l.actualizado_en)}</span>
    </div>`).join("");
}

function claseBurbuja(m) {
  if (m.remitente === "humano") return "de-humano";
  return m.role === "user" ? "de-cliente" : "de-agente";
}

function pintarConversacion(msgs, alFinal) {
  contador.textContent = msgs.length + " mensajes";
  contenido.innerHTML = msgs.length
    ? msgs.map((m, i) => `
        <div class="burbuja ${claseBurbuja(m)} ${alFinal && i >= msgs.length - 1 ? "nuevo" : ""}">
          ${m.remitente === "humano" ? '<span class="remitente-label">Equipo</span>' : ""}
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

function actualizarControlBot(activo) {
  switchBot.checked = activo;
  estadoBot.textContent = activo ? "Bot activo" : "Modo manual";
}

function abrirChat(telefono) {
  vista = "conversacion"; telActual = telefono; ultimaFirma = "";
  volver.style.display = "block";
  nav.style.display = "none";
  titulo.textContent = telefono;
  controlBot.style.display = "flex";
  cajaResponder.style.display = "flex";
  document.body.classList.add("con-caja");
  const lead = leadsPorTelefono[telefono];
  actualizarControlBot(lead ? lead.bot_activo !== false : true);
  refrescar(true);
}

volver.onclick = () => {
  vista = "chats"; telActual = null; ultimaFirma = "";
  volver.style.display = "none";
  nav.style.display = "flex";
  controlBot.style.display = "none";
  cajaResponder.style.display = "none";
  document.body.classList.remove("con-caja");
  titulo.textContent = "Fran";
  refrescar();
};

switchBot.onchange = async () => {
  const activo = switchBot.checked;
  switchBot.disabled = true;
  try {
    await pedir("/panel/accion/bot/" + encodeURIComponent(telActual), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ activo }),
    });
    actualizarControlBot(activo);
    if (leadsPorTelefono[telActual]) leadsPorTelefono[telActual].bot_activo = activo;
    aviso(activo ? "Fran vuelve a responder este chat" : "Fran ya no responde este chat");
  } catch (e) {
    switchBot.checked = !activo; // revierte el visual si fallo
    aviso(e.message);
  } finally {
    switchBot.disabled = false;
  }
};

function ajustarAlturaTextarea() {
  textoResponder.style.height = "auto";
  textoResponder.style.height = Math.min(textoResponder.scrollHeight, 100) + "px";
}
textoResponder.addEventListener("input", ajustarAlturaTextarea);
textoResponder.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); enviarManual(); }
});

async function enviarManual() {
  const texto = textoResponder.value.trim();
  if (!texto || !telActual) return;
  btnResponder.disabled = true;
  try {
    await pedir("/panel/accion/responder/" + encodeURIComponent(telActual), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texto }),
    });
    textoResponder.value = "";
    ajustarAlturaTextarea();
    ultimaFirma = "";
    refrescar(true);
  } catch (e) {
    aviso(e.message);
  } finally {
    btnResponder.disabled = false;
  }
}

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
      const firma = JSON.stringify(leads.map(l => l.telefono + l.actualizado_en + l.escalado + l.bot_activo));
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

function leerCookie(nombre) {
  const fila = document.cookie.split("; ").find(f => f.startsWith(nombre + "="));
  return fila ? decodeURIComponent(fila.split("=")[1]) : "";
}
$("linkMetricas").href = "/panel/dashboard?token=" + encodeURIComponent(
  new URLSearchParams(location.search).get("token") || leerCookie("panel_token")
);
</script>
</body>
</html>
"""


DASHBOARD_PAGINA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fran — Métricas</title>
<style>
  :root {
    --fondo:#05060a; --panel:#0d1020; --borde:#1c2340; --texto:#e8ecff; --suave:#7c86b8;
    --cian:#00e5ff; --magenta:#ff2fd0; --violeta:#7c5cff; --verde:#39ff9d;
    --sombra-cian:0 0 18px rgba(0,229,255,.35); --sombra-magenta:0 0 18px rgba(255,47,208,.3);
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body {
    margin:0; min-height:100vh; color:var(--texto);
    font:14.5px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif;
    background:
      radial-gradient(circle at 15% 0%, rgba(124,92,255,.16), transparent 45%),
      radial-gradient(circle at 90% 15%, rgba(0,229,255,.13), transparent 40%),
      repeating-linear-gradient(0deg, rgba(255,255,255,.025) 0 1px, transparent 1px 42px),
      repeating-linear-gradient(90deg, rgba(255,255,255,.025) 0 1px, transparent 1px 42px),
      var(--fondo);
    padding-bottom:36px;
  }
  header { position:sticky; top:0; z-index:10; backdrop-filter:blur(10px);
    background:rgba(5,6,10,.82); border-bottom:1px solid var(--borde); }
  .barra { max-width:980px; margin:0 auto; display:flex; align-items:center; gap:12px; padding:16px 18px; }
  .volver { color:var(--suave); text-decoration:none; font-size:20px; line-height:1; flex-shrink:0; }
  .titulos { flex:1; min-width:0; }
  .titulos h1 {
    margin:0; font-size:18px; font-weight:700; letter-spacing:.5px;
    background:linear-gradient(90deg, var(--cian), var(--violeta) 60%, var(--magenta));
    -webkit-background-clip:text; background-clip:text; color:transparent;
  }
  .titulos p { margin:2px 0 0; font-size:11.5px; color:var(--suave); letter-spacing:.4px; text-transform:uppercase; }
  .vivo { display:flex; align-items:center; gap:6px; font-size:11px; color:var(--verde);
    letter-spacing:.5px; text-transform:uppercase; flex-shrink:0; }
  .punto { width:7px; height:7px; border-radius:50%; background:var(--verde);
    box-shadow:0 0 8px var(--verde); animation:latido 1.8s infinite; }
  @keyframes latido { 0%,100%{opacity:1} 50%{opacity:.35} }

  main { max-width:980px; margin:0 auto; padding:20px 18px 8px; }

  .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px,1fr)); gap:12px; margin-bottom:22px; }
  .tarjeta {
    background:linear-gradient(160deg, var(--panel), rgba(13,16,32,.6));
    border:1px solid var(--borde); border-radius:14px; padding:16px 16px 14px;
    position:relative; overflow:hidden; transition:border-color .15s, transform .15s;
  }
  .tarjeta::before {
    content:""; position:absolute; inset:0; border-radius:14px; padding:1px;
    background:linear-gradient(135deg, var(--acento,var(--cian)), transparent 40%);
    -webkit-mask:linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite:xor; mask-composite:exclude; opacity:.5; pointer-events:none;
  }
  .tarjeta.c{ --acento:var(--cian) } .tarjeta.m{ --acento:var(--magenta) }
  .tarjeta.v{ --acento:var(--violeta) } .tarjeta.g{ --acento:var(--verde) }
  .tarjeta .etiqueta { font-size:10.5px; color:var(--suave); text-transform:uppercase;
    letter-spacing:.6px; margin-bottom:8px; }
  .tarjeta .valor { font-size:26px; font-weight:700; font-variant-numeric:tabular-nums;
    color:var(--texto); text-shadow:0 0 14px color-mix(in srgb, var(--acento) 55%, transparent); }
  .tarjeta .sub { font-size:11.5px; color:var(--suave); margin-top:4px; }
  .tarjeta .sub b { color:var(--acento); font-weight:700; }

  .seccion { margin-bottom:26px; }
  .seccion h2 { font-size:12.5px; text-transform:uppercase; letter-spacing:.6px;
    color:var(--suave); margin:0 0 12px; font-weight:700; }

  .panelgrafico {
    background:var(--panel); border:1px solid var(--borde); border-radius:14px;
    padding:18px 18px 10px; display:flex; align-items:flex-end; gap:10px; height:150px;
  }
  .barra-dia { flex:1; display:flex; flex-direction:column; align-items:center; gap:8px; height:100%; justify-content:flex-end; }
  .barra-dia .cuerpo {
    width:100%; max-width:34px; border-radius:6px 6px 3px 3px; min-height:3px;
    background:linear-gradient(180deg, var(--cian), var(--violeta));
    box-shadow:0 0 12px rgba(0,229,255,.35); transition:height .5s ease;
  }
  .barra-dia .num { font-size:11px; color:var(--texto); font-weight:600; }
  .barra-dia .etq { font-size:10px; color:var(--suave); text-transform:uppercase; }

  .vacio { text-align:center; color:var(--suave); padding:60px 16px; }
  .error { background:rgba(255,47,90,.12); color:#ff5f7a; padding:12px 14px; border-radius:10px;
    border:1px solid rgba(255,47,90,.3); }
</style>
</head>
<body>
<header>
  <div class="barra">
    <a class="volver" href="/panel" title="Volver a chats">&larr;</a>
    <div class="titulos">
      <h1>FRAN · PANEL DE CONTROL</h1>
      <p>Pampa Shop — métricas en vivo</p>
    </div>
    <span class="vivo"><span class="punto"></span>vivo</span>
  </div>
</header>
<main id="contenido"><p class="vacio">Cargando…</p></main>

<script>
const $ = (id) => document.getElementById(id);
const contenido = $("contenido");
const nf = (n) => Number(n || 0).toLocaleString("es-AR");
const usd = (n) => "US$ " + Number(n || 0).toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
const escapar = (t) => { const d = document.createElement("div"); d.textContent = t ?? ""; return d.innerHTML; };

async function pedir(ruta) {
  const r = await fetch(ruta, { credentials: "same-origin" });
  if (!r.ok) {
    let detalle = "Error " + r.status;
    if (r.status === 401) detalle = "Token invalido";
    else { try { detalle = (await r.json()).detail || detalle; } catch (e) {} }
    throw new Error(detalle);
  }
  return r.json();
}

function pintar(m) {
  const totalTokensHoy = m.uso_hoy.tokens_entrada + m.uso_hoy.tokens_salida + m.uso_hoy.tokens_cache;
  const maxSerie = Math.max(1, ...m.serie_mensajes.map(d => d.mensajes));
  const diasCortos = ["dom","lun","mar","mié","jue","vie","sáb"];

  contenido.innerHTML = `
    <div class="seccion">
      <h2>Mensajes</h2>
      <div class="grid">
        <div class="tarjeta c">
          <div class="etiqueta">Entraron hoy</div>
          <div class="valor">${nf(m.mensajes.cliente_hoy)}</div>
          <div class="sub">histórico: <b>${nf(m.mensajes.cliente_total)}</b></div>
        </div>
        <div class="tarjeta v">
          <div class="etiqueta">Respuestas del bot hoy</div>
          <div class="valor">${nf(m.mensajes.bot_hoy)}</div>
          <div class="sub">histórico: <b>${nf(m.mensajes.bot_total)}</b></div>
        </div>
        <div class="tarjeta m">
          <div class="etiqueta">Escalados a humano</div>
          <div class="valor">${nf(m.escalados.activos)}</div>
          <div class="sub">avisados hoy: <b>${nf(m.escalados.hoy)}</b></div>
        </div>
        <div class="tarjeta g">
          <div class="etiqueta">Leads</div>
          <div class="valor">${nf(m.leads.total)}</div>
          <div class="sub">nuevos hoy: <b>${nf(m.leads.hoy)}</b></div>
        </div>
      </div>
    </div>

    <div class="seccion">
      <h2>Consumo de Claude (estimado)</h2>
      <div class="grid">
        <div class="tarjeta c">
          <div class="etiqueta">Tokens hoy</div>
          <div class="valor">${nf(totalTokensHoy)}</div>
          <div class="sub">${nf(m.uso_hoy.tokens_entrada)} in · ${nf(m.uso_hoy.tokens_salida)} out · ${nf(m.uso_hoy.tokens_cache)} cache</div>
        </div>
        <div class="tarjeta v">
          <div class="etiqueta">Costo hoy</div>
          <div class="valor">${usd(m.uso_hoy.costo_usd)}</div>
          <div class="sub">${nf(m.uso_hoy.respuestas)} respuestas con IA</div>
        </div>
        <div class="tarjeta m">
          <div class="etiqueta">Costo histórico</div>
          <div class="valor">${usd(m.uso_total.costo_usd)}</div>
          <div class="sub">${nf(m.uso_total.respuestas)} respuestas con IA</div>
        </div>
        <div class="tarjeta g">
          <div class="etiqueta">Tokens histórico</div>
          <div class="valor">${nf(m.uso_total.tokens_entrada + m.uso_total.tokens_salida + m.uso_total.tokens_cache)}</div>
          <div class="sub">${nf(m.uso_total.tokens_cache)} servidos desde cache</div>
        </div>
      </div>
    </div>

    <div class="seccion">
      <h2>Mensajes entrantes — últimos ${m.serie_mensajes.length} días</h2>
      <div class="panelgrafico">
        ${m.serie_mensajes.map(d => {
          const alto = Math.round((d.mensajes / maxSerie) * 100);
          const fecha = new Date(d.dia + "T00:00:00");
          return `<div class="barra-dia">
            <span class="num">${d.mensajes}</span>
            <div class="cuerpo" style="height:${Math.max(alto, 3)}%"></div>
            <span class="etq">${diasCortos[fecha.getDay()]}</span>
          </div>`;
        }).join("")}
      </div>
    </div>
  `;
}

async function refrescar() {
  try {
    pintar(await pedir("/panel/datos/metricas"));
  } catch (e) {
    contenido.innerHTML = '<p class="error">' + escapar(e.message) + "</p>";
  }
}

setInterval(refrescar, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refrescar(); });
refrescar();
</script>
</body>
</html>
"""
