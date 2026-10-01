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

from agent.brain import cargar_system_prompt
from agent.memory import (
    alternar_bot,
    borrar_config,
    guardar_config,
    guardar_mensaje,
    listar_borradores_pendientes,
    listar_leads,
    marcar_borrador,
    obtener_config,
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


@router.get("/datos/prompt")
async def datos_prompt(request: Request):
    """
    El prompt que hoy esta en uso (override guardado desde el panel, o el base del
    repo si nadie lo toco nunca) junto con el base, para que el panel pueda mostrar
    si esta personalizado y ofrecer "restaurar el original".
    """
    _verificar(request)
    base = cargar_system_prompt()
    override = await obtener_config("system_prompt")
    return {
        "prompt": override if override else base,
        "personalizado": bool(override) and override != base,
    }


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


@router.post("/accion/prompt")
async def accion_prompt(request: Request, cuerpo: dict = Body(default={})):
    """
    Guarda un system prompt nuevo. Se aplica al toque: generar_respuesta lo relee de
    la base en cada mensaje, asi que no hace falta reiniciar ni redeployar.
    """
    _verificar(request)
    texto = (cuerpo.get("texto") or "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail="El prompt no puede quedar vacio")

    await guardar_config("system_prompt", texto)
    logger.info("System prompt actualizado desde el panel")
    return {"ok": True}


@router.post("/accion/prompt/restaurar")
async def accion_prompt_restaurar(request: Request):
    """Saca el override guardado y vuelve a usar el prompt base de config/prompts.yaml."""
    _verificar(request)
    await borrar_config("system_prompt")
    logger.info("System prompt restaurado al original desde el panel")
    return {"ok": True, "prompt": cargar_system_prompt()}


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
<title>PAMPA</title>
<style>
  :root {
    --app:#fff; --panel:#fff; --borde:#dfe1e8; --texto:#181a25; --suave:#8a8d99; --tenue:#b0b3bf;
    --cliente:#fff; --azul:#3b6fe0; --azul-suave:#eef2ff; --humano:#7c5cff; --humano-suave:#f4f1ff;
    --acento:#3b6fe0; --acento-suave:#eef2ff;
    --verde:#16a34a; --verde-bg:#ecfdf3;
    --alerta:#d0483f; --alerta-bg:#fdf2f1;
    --ambar:#b3760f; --ambar-bg:#fdf6ea;
    --sombra:0 1px 3px rgba(15,23,42,.07), 0 1px 1px rgba(15,23,42,.05);
    --superficie:#f4f5f9;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --app:#0b0d14; --panel:#12141e; --borde:#22242f; --texto:#eceef3; --suave:#8b8e9c; --tenue:#565866;
      --cliente:#191b26; --azul:#5b86ec; --azul-suave:#1a2236; --humano:#8a6bf5; --humano-suave:#211c33;
      --acento:#5b86ec; --acento-suave:#1a2236;
      --verde:#34d399; --verde-bg:#0f2b22;
      --alerta:#e77b73; --alerta-bg:#2a1614;
      --ambar:#e0b256; --ambar-bg:#241d0e;
      --sombra:0 1px 3px rgba(0,0,0,.3);
      --superficie:#171a26;
    }
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html, body { height:100%; }
  body {
    margin:0; background:var(--app); color:var(--texto);
    font:14.5px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }

  .app { display:flex; flex-direction:column; height:100vh; overflow:hidden; }

  /* ── Sidebar (top bar en mobile, columna en desktop) ────────────────── */
  .sidebar {
    flex-shrink:0; display:flex; align-items:center; gap:10px;
    width:100%; padding:9px 12px; background:var(--panel);
    border-bottom:1px solid var(--borde); position:sticky; top:0; z-index:30;
    overflow-x:auto;
  }
  .marca { display:none; }
  .sidebar-nav { display:flex; flex:1; gap:4px; min-width:0; }
  .nav-btn {
    display:flex; align-items:center; gap:6px; flex:1; justify-content:center;
    background:none; border:0; border-radius:9px; padding:8px 6px; font-size:12.5px;
    font-weight:600; color:var(--suave); cursor:pointer; font-family:inherit;
    text-decoration:none; white-space:nowrap;
  }
  .nav-btn span.txt { overflow:hidden; text-overflow:ellipsis; }
  .nav-btn.activa { background:var(--acento-suave); color:var(--acento); }
  .nav-btn .globo {
    background:var(--alerta); color:#fff; border-radius:9px; padding:1px 6px;
    font-size:10px; font-weight:700; display:inline-block;
  }
  .vivo-sidebar { display:none; align-items:center; gap:5px; font-size:11.5px; color:var(--suave); flex-shrink:0; }
  .punto { width:7px; height:7px; border-radius:50%; background:#9ca3af; }
  .punto.on { background:var(--verde); animation:latido 2s infinite; }
  @keyframes latido { 0%{box-shadow:0 0 0 0 rgba(22,163,74,.5)} 70%{box-shadow:0 0 0 6px rgba(22,163,74,0)} 100%{box-shadow:0 0 0 0 rgba(22,163,74,0)} }

  /* ── Panel principal: lista + hilo ───────────────────────────────────── */
  .panel-principal { flex:1; display:flex; min-width:0; min-height:0; }

  .lista-conversaciones {
    width:100%; display:flex; flex-direction:column; min-height:0; background:var(--panel);
  }
  .chats-scroll { overflow-y:auto; padding:8px; flex:1; min-height:0; }

  /* ── Filtro por fecha ─────────────────────────────────────────────────── */
  .filtro-fecha {
    flex-shrink:0; display:flex; align-items:center; gap:6px; flex-wrap:wrap;
    padding:10px 12px; border-bottom:1px solid var(--borde);
  }
  .filtro-fecha input[type="date"] {
    border:1px solid var(--borde); border-radius:8px; padding:5px 7px; font:inherit;
    font-size:12px; background:var(--superficie); color:var(--texto); color-scheme:light;
    min-width:0; flex:1;
  }
  :root:not([data-theme="light"]) .filtro-fecha input[type="date"] { color-scheme:dark; }
  .filtro-fecha .sep { color:var(--tenue); font-size:11px; flex-shrink:0; }
  .filtro-fecha button {
    border:1px solid var(--borde); background:var(--panel); color:var(--suave);
    border-radius:8px; padding:5px 10px; font-size:11.5px; font-weight:600;
    cursor:pointer; font-family:inherit; flex-shrink:0; white-space:nowrap;
  }
  .filtro-fecha button.activo { background:var(--acento-suave); color:var(--acento); border-color:transparent; }
  .filtro-fecha .limpiar { display:none; color:var(--alerta); }
  .filtro-fecha.con-filtro .limpiar { display:inline-block; }

  .item-chat {
    display:flex; gap:12px; align-items:center; padding:11px 12px; border-radius:12px;
    cursor:pointer; border-left:2px solid transparent; margin-bottom:1px;
    transition:background .15s;
  }
  .item-chat:hover { background:var(--superficie); }
  .item-chat.seleccionado { background:var(--azul-suave); }
  .item-chat.espera { border-left-color:var(--alerta); }
  .avatar {
    width:36px; height:36px; border-radius:50%;
    background:linear-gradient(145deg, var(--azul), #6f8fe8);
    color:#fff; display:grid; place-items:center; font-weight:700; font-size:12.5px; flex-shrink:0;
    box-shadow:0 1px 4px rgba(59,111,224,.3);
  }
  .medio { flex:1; min-width:0; }
  .tel { font-weight:700; font-size:13.5px; letter-spacing:-.01em; }
  .ultimo { color:var(--suave); font-size:12.5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:1px; }
  .cuando { color:var(--tenue); font-size:11px; white-space:nowrap; align-self:flex-start; }
  .etiqueta { font-size:9.5px; padding:2px 7px; border-radius:20px; font-weight:600;
    background:var(--alerta-bg); color:var(--alerta); letter-spacing:.2px; }
  .etiqueta.manual { background:var(--ambar-bg); color:var(--ambar); margin-left:4px; }

  .hilo-conversacion { display:none; flex-direction:column; width:100%; min-height:0; background:var(--app); }
  .app.chat-abierto .lista-conversaciones { display:none; }
  .app.chat-abierto .hilo-conversacion { display:flex; }

  .hilo-header {
    flex-shrink:0; display:flex; align-items:center; gap:10px; padding:14px 18px;
    background:var(--panel); border-bottom:1px solid var(--borde);
  }
  .volver { background:none; border:0; color:var(--suave); font-size:19px; cursor:pointer;
    padding:0 2px; line-height:1; transition:color .15s; }
  .volver:hover { color:var(--texto); }
  .hilo-header h2 { font-size:14.5px; margin:0; font-weight:600; letter-spacing:-.01em; flex:1;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

  .controlbot { display:none; align-items:center; gap:8px; font-size:12.5px; color:var(--suave); flex-shrink:0; }
  .switch { position:relative; display:inline-block; width:36px; height:21px; flex-shrink:0; }
  .switch input { opacity:0; width:0; height:0; }
  .slider { position:absolute; cursor:pointer; inset:0; background:#d1d5db;
    transition:.2s; border-radius:22px; }
  .slider:before { content:""; position:absolute; height:15px; width:15px; left:3px; bottom:3px;
    background:#fff; transition:.2s; border-radius:50%; }
  .switch input:checked + .slider { background:var(--verde); }
  .switch input:checked + .slider:before { transform:translateX(15px); }

  .hilo-mensajes { flex:1; min-height:0; overflow-y:auto; padding:20px 18px; display:flex; flex-direction:column; }
  .fila-msj { display:flex; flex-direction:column; max-width:76%; margin-bottom:16px; }
  .fila-msj.cliente { align-self:flex-start; align-items:flex-start; }
  .fila-msj.bot, .fila-msj.humano { align-self:flex-end; align-items:flex-end; }
  .etiqueta-msj { font-size:10.5px; color:var(--tenue); margin-bottom:5px; font-weight:600; letter-spacing:.2px; }
  .cuerpo-msj { white-space:pre-wrap; word-wrap:break-word; padding:10px 14px; border-radius:16px;
    box-shadow:var(--sombra); }
  .fila-msj.cliente .cuerpo-msj { background:var(--panel); border:1px solid var(--borde);
    color:var(--texto); border-bottom-left-radius:4px; }
  .fila-msj.bot .cuerpo-msj { background:var(--azul); color:#fff; border-bottom-right-radius:4px; }
  .fila-msj.humano .cuerpo-msj { background:var(--humano); color:#fff; border-bottom-right-radius:4px; }
  .fila-msj.nuevo { animation:entra .3s ease-out; }
  @keyframes entra { from{opacity:0; transform:translateY(6px)} to{opacity:1; transform:none} }

  .aviso-bot { display:none; flex-shrink:0; align-items:center; gap:7px; margin:0 18px 14px;
    padding:9px 13px; font-size:12px; border-radius:11px;
    background:var(--ambar-bg); color:var(--ambar); }

  .caja-responder { display:none; flex-shrink:0; gap:8px; align-items:flex-end;
    padding:12px 16px calc(12px + env(safe-area-inset-bottom));
    background:var(--panel); border-top:1px solid var(--borde); }
  .caja-responder textarea { flex:1; border:1px solid var(--borde); border-radius:18px;
    padding:9px 14px; font:inherit; font-size:14px; resize:none; min-height:20px; max-height:100px;
    background:var(--superficie); color:var(--texto); transition:border-color .15s, background .15s; }
  .caja-responder textarea:focus { outline:none; border-color:var(--azul); background:var(--panel); }
  .caja-responder button { background:var(--azul); color:#fff; border:0; border-radius:18px;
    padding:9px 18px; font-weight:600; cursor:pointer; font-family:inherit; flex-shrink:0; }
  .caja-responder button:disabled { opacity:.5; cursor:default; }

  /* ── Vista de borradores ─────────────────────────────────────────────── */
  .vista-borradores { display:none; flex-direction:column; flex:1; min-height:0; overflow-y:auto;
    padding:18px 16px; background:var(--app); }
  .borradores-header { display:flex; align-items:baseline; gap:10px; margin-bottom:14px; }
  .borradores-header h2 { margin:0; font-size:16px; }
  .borradores-header span { font-size:12.5px; color:var(--suave); }

  .tarjeta { background:var(--panel); border:1px solid var(--borde); border-radius:14px;
    padding:16px; margin-bottom:12px; box-shadow:var(--sombra); }
  .tarjeta .de { font-size:12px; color:var(--suave); margin-bottom:9px; }
  .dijo { background:var(--superficie); border-left:3px solid var(--suave); padding:9px 11px;
    border-radius:0 8px 8px 0; margin-bottom:11px; font-size:14px; }
  .dijo b { display:block; font-size:11px; color:var(--suave); text-transform:uppercase;
    letter-spacing:.4px; margin-bottom:3px; font-weight:700; }
  textarea.txt-borrador {
    width:100%; border:1px solid var(--borde); border-radius:9px; padding:10px 11px;
    font:inherit; font-size:14px; background:var(--azul-suave); color:var(--texto);
    resize:vertical; min-height:90px;
  }
  .acciones { display:flex; gap:9px; margin-top:11px; }
  .acciones button { flex:1; border:0; border-radius:9px; padding:11px; font-size:14.5px;
    font-weight:600; cursor:pointer; font-family:inherit; }
  .enviar { background:var(--azul); color:#fff; }
  .tirar { background:var(--app); color:var(--suave); border:1px solid var(--borde) !important; }
  .acciones button:disabled { opacity:.5; cursor:default; }

  .vacio { text-align:center; color:var(--suave); padding:56px 16px; }
  .vacio .icono { font-size:40px; display:block; margin-bottom:10px; opacity:.5; }

  /* ── Vista de prompt ──────────────────────────────────────────────────── */
  .vista-prompt { display:none; flex-direction:column; flex:1; min-height:0; overflow-y:auto;
    padding:18px 16px; background:var(--app); }
  .prompt-header { display:flex; align-items:baseline; gap:10px; margin-bottom:6px; flex-wrap:wrap; }
  .prompt-header h2 { margin:0; font-size:16px; }
  .prompt-pill { font-size:11px; font-weight:700; padding:2px 9px; border-radius:20px;
    background:var(--acento-suave); color:var(--acento); display:none; }
  .prompt-pill.mostrar { display:inline-block; }
  .prompt-ayuda { font-size:12.5px; color:var(--suave); margin:0 0 14px; line-height:1.5; }
  textarea.txt-prompt {
    flex:1; width:100%; min-height:320px; border:1px solid var(--borde); border-radius:12px;
    padding:14px 16px; font:13.5px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    background:var(--panel); color:var(--texto); resize:vertical; box-shadow:var(--sombra);
  }
  textarea.txt-prompt:focus { outline:none; border-color:var(--azul); }
  .prompt-acciones { display:flex; gap:9px; margin-top:12px; flex-wrap:wrap; }
  .prompt-acciones button { border:0; border-radius:9px; padding:11px 18px; font-size:14px;
    font-weight:600; cursor:pointer; font-family:inherit; }
  .prompt-acciones button:disabled { opacity:.5; cursor:default; }
  .guardar-prompt { background:var(--azul); color:#fff; }
  .restaurar-prompt { background:var(--app); color:var(--suave); border:1px solid var(--borde) !important; }

  .aviso { position:fixed; top:16px; right:16px; padding:10px 16px; border-radius:12px;
    font-size:13px; font-weight:600; box-shadow:0 8px 24px rgba(15,23,42,.12); z-index:60;
    animation:entra .2s ease-out; max-width:320px; }
  .aviso-ok { background:var(--verde-bg); color:var(--verde); border:1px solid rgba(22,163,74,.2); }
  .aviso-error { background:var(--alerta-bg); color:var(--alerta); border:1px solid rgba(208,72,63,.2); }

  /* ── Desktop: sidebar vertical + lista y hilo siempre visibles juntos ── */
  @media (min-width: 881px) {
    .app { flex-direction:row; }
    .sidebar { flex-direction:column; align-items:stretch; width:230px; height:100vh;
      border-bottom:0; border-right:1px solid var(--borde); padding:20px 14px; position:static; }
    .marca { display:block; margin-bottom:22px; }
    .negocio-label { font-size:10.5px; color:var(--suave); letter-spacing:.6px; text-transform:uppercase; }
    .negocio-nombre { font-size:16px; font-weight:700; margin-top:2px; }
    .sidebar-nav { flex-direction:column; gap:3px; }
    .nav-btn { flex:0 0 auto; justify-content:flex-start; padding:9px 10px; font-size:13.5px; }
    .vivo-sidebar { display:flex; margin-top:auto; padding-top:16px; }

    .panel-principal { display:flex !important; }
    .lista-conversaciones { width:320px; flex-shrink:0; border-right:1px solid var(--borde);
      display:flex !important; }
    .hilo-conversacion { display:flex !important; }
    .volver { display:none; }

    /* Borradores y Prompt son vistas de ancho completo: en desktop, sin esto, el
       panel de conversaciones (fijo con !important arriba) quedaria apretujado
       al lado en vez de ceder todo el espacio. */
    .app.vista-secundaria .panel-principal { display:none !important; }
  }
</style>
</head>
<body>
<div class="app" id="app">
  <aside class="sidebar">
    <div class="marca">
      <div class="negocio-label">Negocio</div>
      <div class="negocio-nombre">Pampa Shop</div>
    </div>
    <nav class="sidebar-nav" id="navLateral">
      <button class="nav-btn activa" data-vista="chats">💬 <span class="txt">Conversaciones</span></button>
      <button class="nav-btn" data-vista="borradores">📝 <span class="txt">Por aprobar</span><span class="globo" id="globo" style="display:none">0</span></button>
      <button class="nav-btn" data-vista="prompt">⚙️ <span class="txt">Prompt</span></button>
      <a class="nav-btn" id="linkMetricas" href="#">📊 <span class="txt">Métricas</span></a>
    </nav>
    <div class="vivo-sidebar"><span class="punto" id="punto"></span><span id="contador"></span></div>
  </aside>

  <div class="panel-principal" id="panelPrincipal">
    <div class="lista-conversaciones" id="listaConversaciones">
      <div class="filtro-fecha" id="filtroFecha">
        <button type="button" id="btnFiltroHoy">Hoy</button>
        <button type="button" id="btnFiltroAyer">Ayer</button>
        <button type="button" id="btnFiltro7d">7 días</button>
        <input type="date" id="filtroDesde" aria-label="Desde">
        <span class="sep">–</span>
        <input type="date" id="filtroHasta" aria-label="Hasta">
        <button type="button" class="limpiar" id="btnFiltroLimpiar">✕ Limpiar</button>
      </div>
      <div class="chats-scroll" id="chatsScroll"><p class="vacio">Cargando…</p></div>
    </div>

    <div class="hilo-conversacion" id="hiloConversacion">
      <div class="hilo-header">
        <button class="volver" id="volver" aria-label="Volver">&larr;</button>
        <h2 id="tituloConv">Elegí una conversación</h2>
        <div class="controlbot" id="controlBot">
          <label class="switch">
            <input type="checkbox" id="switchBot">
            <span class="slider"></span>
          </label>
          <span id="estadoBot">Bot activo</span>
        </div>
      </div>
      <div class="hilo-mensajes" id="hiloMensajes">
        <p class="vacio"><span class="icono">&#128172;</span>Elegí un chat de la lista</p>
      </div>
      <div class="aviso-bot" id="avisoBot">
        ⚠️ El bot está activo y podría responder al próximo mensaje del cliente. Pausalo si querés atención exclusivamente humana.
      </div>
      <div class="caja-responder" id="cajaResponder">
        <textarea id="textoResponder" placeholder="Escribir como humano…" rows="1"></textarea>
        <button id="btnResponder" onclick="enviarManual()">Enviar</button>
      </div>
    </div>
  </div>

  <div class="vista-borradores" id="vistaBorradores">
    <div class="borradores-header">
      <h2>Por aprobar</h2>
      <span id="contadorBorr"></span>
    </div>
    <div id="listaBorradores"><p class="vacio">Cargando…</p></div>
  </div>

  <div class="vista-prompt" id="vistaPrompt">
    <div class="prompt-header">
      <h2>Comportamiento del agente</h2>
      <span class="prompt-pill" id="pillPersonalizado">Personalizado</span>
    </div>
    <p class="prompt-ayuda">
      Esto es lo que le dice a PAMPA quién es, cómo hablar y qué reglas seguir. Se aplica
      al instante a partir del próximo mensaje — no hace falta reiniciar nada. Escribí
      con cuidado: un cambio acá afecta TODAS las conversaciones nuevas.
    </p>
    <textarea class="txt-prompt" id="textoPrompt" placeholder="Cargando…" spellcheck="false"></textarea>
    <div class="prompt-acciones">
      <button class="guardar-prompt" id="btnGuardarPrompt" onclick="guardarPrompt()">Guardar cambios</button>
      <button class="restaurar-prompt" id="btnRestaurarPrompt" onclick="restaurarPrompt()">Restaurar original</button>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const app = $("app");
const navLateral = $("navLateral"), globo = $("globo"), punto = $("punto"), contador = $("contador");
const panelPrincipal = $("panelPrincipal"), listaConversaciones = $("listaConversaciones");
const chatsScroll = $("chatsScroll");
const filtroFechaEl = $("filtroFecha"), filtroDesde = $("filtroDesde"), filtroHasta = $("filtroHasta");
const btnFiltroHoy = $("btnFiltroHoy"), btnFiltroAyer = $("btnFiltroAyer"), btnFiltro7d = $("btnFiltro7d");
const btnFiltroLimpiar = $("btnFiltroLimpiar");
const hiloConversacion = $("hiloConversacion"), tituloConv = $("tituloConv"), volver = $("volver");
const controlBot = $("controlBot"), switchBot = $("switchBot"), estadoBot = $("estadoBot"), avisoBot = $("avisoBot");
const hiloMensajes = $("hiloMensajes");
const cajaResponder = $("cajaResponder"), textoResponder = $("textoResponder"), btnResponder = $("btnResponder");
const vistaBorradores = $("vistaBorradores"), listaBorradores = $("listaBorradores"), contadorBorr = $("contadorBorr");
const vistaPrompt = $("vistaPrompt"), textoPrompt = $("textoPrompt"), pillPersonalizado = $("pillPersonalizado");
const btnGuardarPrompt = $("btnGuardarPrompt"), btnRestaurarPrompt = $("btnRestaurarPrompt");

let vista = "chats";        // chats | borradores | prompt | conversacion
let telActual = null;
let ultimaFirmaLista = "";  // firma de la lista de chats (se repinta sola, no depende de "vista")
let ultimaFirma = "";       // firma de la conversacion abierta
let ultimaFirmaBorr = "";   // firma de la lista de borradores
let leadsPorTelefono = {};  // cache de la ultima lista de leads, para leer bot_activo al abrir un chat
let ultimosLeads = [];      // la ultima lista COMPLETA (sin filtrar) que trajo el servidor
let filtroDesdeVal = "";    // filtro de fecha de la lista de chats: "" = sin filtro
let filtroHastaVal = "";
let promptCargado = false;  // para no pisar lo que esta tipeando con cada polling de 4s

const escapar = (t) => { const d = document.createElement("div"); d.textContent = t ?? ""; return d.innerHTML; };
const iniciales = (tel) => String(tel).slice(-2);
const estaAbajo = (el) => el.scrollTop + el.clientHeight >= el.scrollHeight - 120;

function fecha(iso) {
  if (!iso) return "";
  const d = new Date(iso), hoy = new Date();
  const hora = d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === hoy.toDateString()) return hora;
  const ayer = new Date(hoy); ayer.setDate(hoy.getDate() - 1);
  if (d.toDateString() === ayer.toDateString()) return "ayer " + hora;
  return d.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit" }) + " " + hora;
}

let ultimoErrorMostrado = "";
function aviso(texto, tipo) {
  if (tipo === "error") {
    if (texto === ultimoErrorMostrado) return;  // no spamear el mismo error cada 4s
    ultimoErrorMostrado = texto;
    setTimeout(() => { if (ultimoErrorMostrado === texto) ultimoErrorMostrado = ""; }, 4000);
  }
  const d = document.createElement("div");
  d.className = "aviso " + (tipo === "error" ? "aviso-error" : "aviso-ok");
  d.textContent = (tipo === "error" ? "⚠️ " : "✅ ") + texto;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 2800);
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

// ── Filtro por fecha de la lista de chats ───────────────────────────────────
// Filtra por la fecha LOCAL de "actualizado_en" (el dia del ultimo mensaje), no
// por UTC: asi "Hoy" coincide con lo que el humano que mira el panel considera
// "hoy", igual que ya hace fecha() para las etiquetas "ayer"/hora de cada chat.

function diaLocal(d) {
  const dt = d instanceof Date ? d : new Date(d);
  const y = dt.getFullYear(), m = String(dt.getMonth() + 1).padStart(2, "0"), day = String(dt.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
function hoyLocal() { return diaLocal(new Date()); }
function sumarDias(diaStr, n) {
  const [y, m, d] = diaStr.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  dt.setDate(dt.getDate() + n);
  return diaLocal(dt);
}

function aplicarFiltroFecha(leads) {
  if (!filtroDesdeVal && !filtroHastaVal) return leads;
  return leads.filter(l => {
    const dia = diaLocal(l.actualizado_en);
    if (filtroDesdeVal && dia < filtroDesdeVal) return false;
    if (filtroHastaVal && dia > filtroHastaVal) return false;
    return true;
  });
}

function actualizarEstadoFiltro() {
  filtroDesdeVal = filtroDesde.value;
  filtroHastaVal = filtroHasta.value;
  const hoy = hoyLocal(), ayer = sumarDias(hoy, -1), hace7 = sumarDias(hoy, -6);
  btnFiltroHoy.classList.toggle("activo", filtroDesdeVal === hoy && filtroHastaVal === hoy);
  btnFiltroAyer.classList.toggle("activo", filtroDesdeVal === ayer && filtroHastaVal === ayer);
  btnFiltro7d.classList.toggle("activo", filtroDesdeVal === hace7 && filtroHastaVal === hoy);
  filtroFechaEl.classList.toggle("con-filtro", !!(filtroDesdeVal || filtroHastaVal));
  pintarChats(ultimosLeads);  // re-pinta al toque con lo que ya esta en cache, sin esperar al polling
}

function setRangoFiltro(desde, hasta) {
  filtroDesde.value = desde;
  filtroHasta.value = hasta;
  actualizarEstadoFiltro();
}

btnFiltroHoy.addEventListener("click", () => { const h = hoyLocal(); setRangoFiltro(h, h); });
btnFiltroAyer.addEventListener("click", () => { const a = sumarDias(hoyLocal(), -1); setRangoFiltro(a, a); });
btnFiltro7d.addEventListener("click", () => setRangoFiltro(sumarDias(hoyLocal(), -6), hoyLocal()));
btnFiltroLimpiar.addEventListener("click", () => setRangoFiltro("", ""));
filtroDesde.addEventListener("change", actualizarEstadoFiltro);
filtroHasta.addEventListener("change", actualizarEstadoFiltro);

function pintarChats(leads) {
  ultimosLeads = leads;
  leadsPorTelefono = {};
  leads.forEach(l => { leadsPorTelefono[l.telefono] = l; });

  const visibles = aplicarFiltroFecha(leads);
  contador.textContent = visibles.length + (visibles.length === 1 ? " chat" : " chats");
  if (!visibles.length) {
    chatsScroll.innerHTML = leads.length
      ? '<p class="vacio"><span class="icono">&#128197;</span>Nadie escribió en ese rango de fechas.</p>'
      : '<p class="vacio"><span class="icono">&#128172;</span>Todavía no escribió nadie.</p>';
    return;
  }
  // Los que necesitan atencion (esperan una persona, o ya la tienen atendiendo a
  // mano) van arriba.
  const necesitaAtencion = (l) => l.escalado || !l.bot_activo;
  const orden = visibles.slice().sort((a, b) => necesitaAtencion(b) - necesitaAtencion(a));
  chatsScroll.innerHTML = orden.map(l => `
    <div class="item-chat ${necesitaAtencion(l) ? "espera" : ""} ${l.telefono === telActual ? "seleccionado" : ""}"
         onclick="abrirChat('${escapar(l.telefono)}')">
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

function pintarConversacion(msgs, alFinal) {
  hiloMensajes.innerHTML = msgs.length
    ? msgs.map((m, i) => {
        const tipo = m.remitente === "humano" ? "humano" : (m.role === "user" ? "cliente" : "bot");
        const etiqueta = tipo === "cliente" ? "Cliente" : (tipo === "humano" ? "Equipo" : "PAMPA (IA)");
        const esNuevo = alFinal && i >= msgs.length - 1;
        return `<div class="fila-msj ${tipo} ${esNuevo ? "nuevo" : ""}">
          <span class="etiqueta-msj">${etiqueta} · ${fecha(m.timestamp)}</span>
          <div class="cuerpo-msj">${escapar(m.content)}</div>
        </div>`;
      }).join("")
    : '<p class="vacio"><span class="icono">&#128172;</span>Sin mensajes guardados.</p>';
}

function pintarBorradores(bs) {
  contadorBorr.textContent = bs.length ? bs.length + " esperando" : "al día";
  if (!bs.length) {
    listaBorradores.innerHTML = '<p class="vacio"><span class="icono">&#9989;</span>No hay nada para aprobar.</p>';
    return;
  }
  listaBorradores.innerHTML = bs.map(b => `
    <div class="tarjeta" id="b${b.id}">
      <div class="de"><b>${escapar(b.telefono)}</b> &middot; ${fecha(b.creado_en)}</div>
      <div class="dijo"><b>El cliente escribió</b>${escapar(b.mensaje_cliente)}</div>
      <textarea class="txt-borrador" id="t${b.id}">${escapar(b.respuesta)}</textarea>
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
    ultimaFirmaBorr = "";
    refrescar();
  } catch (e) {
    botones.forEach(b => b.disabled = false);
    aviso(e.message, "error");
  }
}

function actualizarControlBot(activo) {
  switchBot.checked = activo;
  estadoBot.textContent = activo ? "Bot activo" : "Modo manual";
  avisoBot.style.display = activo ? "flex" : "none";
}

function abrirChat(telefono) {
  vista = "conversacion"; telActual = telefono; ultimaFirma = "";
  app.classList.add("chat-abierto");
  tituloConv.textContent = telefono;
  controlBot.style.display = "flex";
  cajaResponder.style.display = "flex";
  const lead = leadsPorTelefono[telefono];
  actualizarControlBot(lead ? lead.bot_activo !== false : true);
  pintarChats(Object.values(leadsPorTelefono));  // repinta para resaltar el seleccionado
  refrescar(true);
}

function cerrarChat() {
  telActual = null; ultimaFirma = "";
  app.classList.remove("chat-abierto");
  tituloConv.textContent = "Elegí una conversación";
  controlBot.style.display = "none";
  cajaResponder.style.display = "none";
  avisoBot.style.display = "none";
  hiloMensajes.innerHTML = '<p class="vacio"><span class="icono">&#128172;</span>Elegí un chat de la lista</p>';
}

volver.onclick = () => { vista = "chats"; cerrarChat(); refrescar(); };

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
    aviso(activo ? "Bot activo" : "Bot pausado");
  } catch (e) {
    switchBot.checked = !activo; // revierte el visual si fallo
    aviso(e.message, "error");
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
    aviso(e.message, "error");
  } finally {
    btnResponder.disabled = false;
  }
}

async function cargarPrompt() {
  const d = await pedir("/panel/datos/prompt");
  textoPrompt.value = d.prompt;
  pillPersonalizado.classList.toggle("mostrar", d.personalizado);
  promptCargado = true;
}

async function guardarPrompt() {
  const texto = textoPrompt.value.trim();
  if (!texto) { aviso("El prompt no puede quedar vacío", "error"); return; }
  btnGuardarPrompt.disabled = true;
  try {
    await pedir("/panel/accion/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texto }),
    });
    pillPersonalizado.classList.add("mostrar");
    aviso("Prompt actualizado — ya está en uso");
  } catch (e) {
    aviso(e.message, "error");
  } finally {
    btnGuardarPrompt.disabled = false;
  }
}

async function restaurarPrompt() {
  if (!confirm("¿Volver al prompt original del repositorio? Se pierde lo que hayas personalizado.")) return;
  btnRestaurarPrompt.disabled = true;
  try {
    const d = await pedir("/panel/accion/prompt/restaurar", { method: "POST" });
    textoPrompt.value = d.prompt;
    pillPersonalizado.classList.remove("mostrar");
    aviso("Prompt restaurado al original");
  } catch (e) {
    aviso(e.message, "error");
  } finally {
    btnRestaurarPrompt.disabled = false;
  }
}

navLateral.addEventListener("click", (e) => {
  const boton = e.target.closest(".nav-btn[data-vista]");
  if (!boton) return;
  navLateral.querySelectorAll(".nav-btn[data-vista]").forEach(b => b.classList.remove("activa"));
  boton.classList.add("activa");
  vista = boton.dataset.vista;
  app.classList.toggle("vista-secundaria", vista === "borradores" || vista === "prompt");
  if (vista !== "conversacion") cerrarChat();
  if (vista === "prompt" && !promptCargado) cargarPrompt().catch(e => aviso(e.message, "error"));
  refrescar();
});

// ── Refresco en vivo ───────────────────────────────────────────────────────
// Cada 4 segundos, y solo se repinta lo que cambio (se compara una firma por
// panel): repintar siempre reiniciaria el scroll y perderia lo que se este
// editando en un borrador.

async function refrescar(forzarAbajo) {
  if (document.hidden) return;
  punto.classList.add("on");
  try {
    if (vista === "borradores") {
      panelPrincipal.style.display = "none";
      vistaBorradores.style.display = "flex";
      vistaPrompt.style.display = "none";
      const bs = await pedir("/panel/datos/borradores");
      globo.textContent = bs.length; globo.style.display = bs.length ? "inline-block" : "none";
      const firma = JSON.stringify(bs.map(b => b.id));
      if (firma !== ultimaFirmaBorr) { pintarBorradores(bs); ultimaFirmaBorr = firma; }
      return;
    }

    if (vista === "prompt") {
      // No se re-pide cada 4s a propósito: pisaría lo que la persona esté tipeando.
      // Se carga una sola vez al entrar (ver el click handler de navLateral).
      panelPrincipal.style.display = "none";
      vistaBorradores.style.display = "none";
      vistaPrompt.style.display = "flex";
      return;
    }

    panelPrincipal.style.display = "flex";
    vistaBorradores.style.display = "none";
    vistaPrompt.style.display = "none";

    // La lista de chats se mantiene al dia siempre: en desktop queda visible al
    // lado del hilo, y en mobile es la pantalla de cuando no hay chat abierto.
    const [leads, bs] = await Promise.all([
      pedir("/panel/datos/leads"),
      pedir("/panel/datos/borradores").catch(() => []),
    ]);
    globo.textContent = bs.length; globo.style.display = bs.length ? "inline-block" : "none";
    const firmaLista = JSON.stringify(leads.map(l => l.telefono + l.actualizado_en + l.escalado + l.bot_activo));
    if (firmaLista !== ultimaFirmaLista) { pintarChats(leads); ultimaFirmaLista = firmaLista; }

    if (vista === "conversacion" && telActual) {
      const msgs = await pedir("/panel/datos/conversacion/" + encodeURIComponent(telActual));
      const firma = JSON.stringify(msgs.map(m => m.timestamp + m.content.length));
      if (firma !== ultimaFirma) {
        const abajo = forzarAbajo || estaAbajo(hiloMensajes);
        pintarConversacion(msgs, ultimaFirma !== "");
        ultimaFirma = firma;
        if (abajo) hiloMensajes.scrollTop = hiloMensajes.scrollHeight;
      }
    }
  } catch (e) {
    aviso(e.message, "error");
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
<title>PAMPA — Métricas</title>
<style>
  :root {
    --fondo:#fff; --panel:#fff; --borde:#dfe1e8;
    --texto:#181a25; --suave:#8a8d99; --tenue:#b0b3bf;
    --acento:#3b6fe0; --acento-suave:#eef2ff;
    --verde:#16a34a;
    --sombra:0 1px 3px rgba(15,23,42,.07), 0 1px 1px rgba(15,23,42,.05);
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body {
    margin:0; min-height:100vh; color:var(--texto);
    font:14.5px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    background:var(--fondo);
    padding-bottom:48px;
  }
  header { position:sticky; top:0; z-index:10; backdrop-filter:blur(14px);
    background:rgba(255,255,255,.85); border-bottom:1px solid var(--borde); }
  .barra { max-width:900px; margin:0 auto; display:flex; align-items:center; gap:14px; padding:20px 22px; }
  .volver { color:var(--suave); text-decoration:none; font-size:18px; line-height:1; flex-shrink:0;
    transition:color .15s; }
  .volver:hover { color:var(--texto); }
  .titulos { flex:1; min-width:0; }
  .titulos h1 {
    margin:0; font-size:16.5px; font-weight:600; letter-spacing:.2px; color:var(--texto);
  }
  .titulos h1 b { color:var(--acento); font-weight:600; }
  .titulos p { margin:3px 0 0; font-size:11.5px; color:var(--tenue); letter-spacing:.5px; }
  .vivo { display:flex; align-items:center; gap:6px; font-size:11px; color:var(--suave);
    letter-spacing:.4px; flex-shrink:0; }
  .punto { width:6px; height:6px; border-radius:50%; background:var(--verde);
    animation:latido 2.4s infinite; }
  @keyframes latido { 0%,100%{opacity:.9} 50%{opacity:.35} }

  main { max-width:900px; margin:0 auto; padding:28px 22px 8px; }

  .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(160px,1fr)); gap:14px; margin-bottom:34px; }
  .tarjeta {
    background:var(--panel); border:1px solid var(--borde); border-radius:16px;
    padding:20px 20px 18px; box-shadow:var(--sombra); transition:border-color .2s, transform .15s;
  }
  .tarjeta:hover { border-color:#c7cbd6; transform:translateY(-1px); }
  .tarjeta .etiqueta { font-size:10.5px; color:var(--tenue); text-transform:uppercase;
    letter-spacing:1px; font-weight:600; margin-bottom:10px; }
  .tarjeta .valor { font-size:28px; font-weight:600; font-variant-numeric:tabular-nums;
    letter-spacing:-.01em; color:var(--texto); }
  .tarjeta .sub { font-size:12px; color:var(--tenue); margin-top:7px; }
  .tarjeta .sub b { color:var(--suave); font-weight:600; }

  .seccion { margin-bottom:34px; }
  .seccion h2 { font-size:11px; text-transform:uppercase; letter-spacing:1px;
    color:var(--tenue); margin:0 0 14px; font-weight:600; padding-bottom:10px;
    border-bottom:1px solid var(--borde); }

  .panelgrafico {
    background:var(--panel); border:1px solid var(--borde); border-radius:16px;
    padding:22px 22px 14px; display:flex; align-items:flex-end; gap:14px; height:150px;
    box-shadow:var(--sombra);
  }
  .barra-dia { flex:1; display:flex; flex-direction:column; align-items:center; gap:9px; height:100%; justify-content:flex-end; }
  .barra-dia .cuerpo {
    width:100%; max-width:26px; border-radius:5px 5px 2px 2px; min-height:3px;
    background:linear-gradient(180deg, var(--acento), #a9c0f5);
    transition:height .5s ease;
  }
  .barra-dia .num { font-size:11px; color:var(--suave); font-weight:600; font-variant-numeric:tabular-nums; }
  .barra-dia .etq { font-size:10px; color:var(--tenue); text-transform:uppercase; letter-spacing:.4px; }

  .vacio { text-align:center; color:var(--tenue); padding:60px 16px; }
  .error { background:#fdf2f1; color:#d0483f; padding:12px 14px; border-radius:12px;
    border:1px solid rgba(208,72,63,.2); }
</style>
</head>
<body>
<header>
  <div class="barra">
    <a class="volver" href="/panel" title="Volver a chats">&larr;</a>
    <div class="titulos">
      <h1><b>PAMPA</b> — Panel de control</h1>
      <p>Pampa Shop · métricas en vivo</p>
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
