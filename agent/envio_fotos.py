# agent/envio_fotos.py — Envio de las fotos de producto que el bot decidio mandar

"""
Lo comparten main.py (respuesta automatica) y panel.py (cuando alguien aprueba un
borrador que traia fotos): los dos tienen que mandarlas igual, despues del texto.
"""

import logging

logger = logging.getLogger("agentkit")


async def enviar_fotos(proveedor, telefono: str, fotos: list[dict], contexto: dict | None = None) -> list[dict]:
    """
    Manda las fotos de a una y en orden (esperando cada respuesta, asi llegan en el orden
    en que se mandaron). Devuelve las que salieron bien. Una foto que falla NO corta las
    demas ni tira abajo la respuesta: el cliente ya recibio el texto, lo peor que pasa es
    que le falte una foto.
    """
    enviadas = []
    for foto in fotos:
        try:
            ok = await proveedor.enviar_imagen(telefono, foto["url"], foto.get("descripcion", ""), contexto)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error mandando una foto a {telefono}: {e}")
            continue
        if ok:
            enviadas.append(foto)
        else:
            logger.warning(f"No se pudo mandar la foto {foto['url'][-60:]} a {telefono}")
    return enviadas


def nota_de_fotos(enviadas: list[dict]) -> str:
    """
    Linea para dejar en el historial, asi el modelo (y el panel) saben que se mandaron
    fotos. Ej: '[Se enviaron 3 fotos por WhatsApp: Zapato Vizzano 11851152 Pelica]'.
    """
    if not enviadas:
        return ""
    nombres = list(dict.fromkeys(f["descripcion"] for f in enviadas if f.get("descripcion")))
    detalle = f": {'; '.join(nombres)}" if nombres else ""
    return f"[Se enviaron {len(enviadas)} fotos por WhatsApp{detalle}]"
