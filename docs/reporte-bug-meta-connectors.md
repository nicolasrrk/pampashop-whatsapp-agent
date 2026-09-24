# Reporte de bug — creación de herramientas en Meta Business Agent Connectors

## Datos de la cuenta

- WhatsApp Business Phone Number ID (entity_id): `1296717350197497`
- WABA: `1617520096682634` ("Pampa Minorista")
- App: `2497902220658807` ("Pampashop Ads CLI")
- Connector afectado: `tiendanubepampashop`
  (id: `pfbid06wLrGhzxtZPtFWdvCzM3NJPNFhU4FGWRNtxkK5hTZ4NUzEBLLpono9FXPVMaACwmTgNh2sc48Dz6xtw2DioN8wnkNJJVzCvL7K7dl`)

## Resumen

`POST /{entity_id}/agent_connectors/{connector_id}/tools` devuelve
`500 {"title":"Failed to create tool","detail":"Failed to create action: Authorization failed"}`
en TODOS los intentos, sin importar si el connector tiene credenciales validas o ninguna.

## Evidencia de que no es un problema de credenciales

1. El connector `tiendanubepampashop` tiene un API Key real de Tienda Nube, verificado por
   fuera de la API de Meta: el mismo token responde `200 OK` contra
   `GET https://api.tiendanube.com/2025-03/6296125/store` y contra `/products`.
2. Un connector de prueba con `auth_type: "NONE"` (sin ninguna credencial) da el MISMO
   error exacto al crear una herramienta.
3. Un segundo connector con las credenciales combinadas de otra forma (token + "Bearer "
   en un solo campo `value`, sin `prefix` separado) da el mismo error tambien.
4. El token de Meta usado tiene el permiso `whatsapp_business_messaging` en sus scopes,
   que la documentacion de este mismo endpoint lista como suficiente (alternativa a las
   capabilities `bizai_wa_enterprise_api_3p_access` / `bizai_ig_enterprise_api_3p_access`).

## Por que parece un bug de orden de validacion

La documentacion de `POST /{connector_id}/tools/{tool_id}/run` dice explicitamente que
sirve para "verify that a tool's request definition and the connector's credentials work
BEFORE relying on the agent to invoke the tool in a live conversation" — es decir, la
prueba en vivo contra el tercero es un paso APARTE y posterior a la creacion.

Pero el error "Authorization failed" aparece ya en el `POST /tools` (creacion), antes de
llegar al paso de `/run`. Esto sugiere que el endpoint de creacion esta intentando
validar en vivo contra el tercero en el momento equivocado, contra su propio diseño
documentado.

## fbtrace_id para revisar

- `Ajr-TkE4QR-v0tJASmJfark` — creacion de tool sobre el connector real (con token valido)
- `A7dlry04gEIPS4AYJqHBiv1` — mismo intento sobre un segundo connector con formato de
  credencial distinto
- `Av3h-AQ6x4z4fM_qLKQUfwA` — un intento anterior con mensaje "Invalid request" en vez de
  "Authorization failed" (parece un error distinto, tal vez relacionado)

## Otros bugs encontrados en la misma sesion, por si sirven de contexto

- `POST /agent_connectors` devuelve `400 "Invalid connector request"` si se envia el campo
  `connector_protocol: "HTTP"` explicito, pese a que la documentacion lo da como valor
  valido y por defecto. Omitiendo el campo, funciona.
- El campo `name` de un connector no acepta espacios ni guiones (`"Tienda Nube PAMPA SHOP"`
  y `"tienda-nube"` fallan con 400; `"tiendanube"` funciona), sin que la documentacion lo
  mencione.
- `POST /agent_connectors/{id}/upsertApiKey` devuelve `500` y NO aplica el cambio: se
  verifico con un GET posterior que la credencial siguio igual a como estaba.
- `DELETE /agent_connectors/{id}` devuelve `500` en la gran mayoria de los intentos (8 de 9
  en esta sesion); funciono una sola vez sin cambiar nada del request.

## Pregunta para soporte

¿Es un bug conocido de la validacion en `POST /tools`? ¿Hay alguna forma de crear una
herramienta sin que dispare la llamada de prueba contra el connector, para poder usar
`/run` como paso de verificacion manual segun el diseño documentado?
