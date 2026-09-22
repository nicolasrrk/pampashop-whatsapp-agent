# Despliegue en Railway — Pampa (PAMPA SHOP)

## 1. Crear el proyecto

1. Entrar a https://railway.app con la cuenta de GitHub.
2. **New Project** → **Deploy from GitHub repo** → elegir
   `nicolasrrk/pampashop-whatsapp-agent`.
3. Railway detecta el `Dockerfile` solo (ya esta forzado en `railway.json`).
   No hay que elegir lenguaje ni comando de arranque.

## 2. Volumen persistente (ANTES del primer deploy)

Sin esto, cada redespliegue borra el historial de todas las conversaciones.

1. En el servicio → **Variables** → pestana **Volumes** → **New Volume**.
2. Mount path: `/app/data`
3. En **Variables**, setear:
   `DATABASE_URL=sqlite+aiosqlite:////app/data/agentkit.db`
   Son CUATRO barras: `////`. Tres son ruta relativa, cuatro son absoluta. Con tres
   la base se crea adentro del contenedor y el volumen queda sin usar.

## 3. Variables de entorno

Copiar desde el `.env` local (Railway tiene "Raw Editor" para pegarlas todas juntas).
NO subir el archivo `.env` al repo: esta en `.gitignore` a proposito.

    GROQ_API_KEY=...
    GROQ_MODEL=openai/gpt-oss-120b
    WHATSAPP_PROVIDER=meta
    META_ACCESS_TOKEN=...          <- el de System User, que no expira
    META_PHONE_NUMBER_ID=1296717350197497
    META_WABA_ID=1617520096682634
    META_VERIFY_TOKEN=pampashop-verify
    META_APP_SECRET=...            <- app "Pampashop Ads CLI" -> Configuracion -> Basica
    META_API_VERSION=v25.0
    TIENDANUBE_STORE_ID=6296125
    TIENDANUBE_ACCESS_TOKEN=...
    TIENDANUBE_API_VERSION=2025-03
    MODO_ENVIO=borrador
    ESCALACION_WHATSAPP_NUMERO=...
    DATABASE_URL=sqlite+aiosqlite:////app/data/agentkit.db
    ENVIRONMENT=production
    PANEL_TOKEN=...             <- clave del panel web; sin esto /panel da 404

NO setear `PORT`: lo inyecta Railway y el Dockerfile ya lo toma.

## 4. Dominio publico

Servicio → **Settings** → **Networking** → **Generate Domain**.
Queda algo como `pampashop-whatsapp-agent-production.up.railway.app`.

Verificar que responde:

    curl https://TU-DOMINIO/

Tiene que devolver el health check con el proveedor en "meta" y el numero conectado.

## 5. Webhook en Meta

https://developers.facebook.com → app **Pampashop Ads CLI** → **WhatsApp** →
**Configuracion** → Webhook → **Editar**:

- Callback URL:  `https://TU-DOMINIO/webhook`
- Verify token:  `pampashop-verify`

Meta hace un GET de verificacion al guardar; si la URL responde, queda en verde.
Despues, en **Campos del webhook**, suscribir **messages**. Sin eso el webhook queda
configurado pero no llega ningun mensaje.

## 6. Probar

1. Escribirle al +54 9 362 410-5684 desde un celular cualquiera.
2. Ver los logs en Railway → pestana **Deployments** → **View Logs**.
3. Como `MODO_ENVIO=borrador`, la respuesta NO sale sola: queda esperando aprobacion.
   Para aprobarla, desde Railway → el servicio → pestana **Settings** → terminal, o
   con la CLI:

       railway run python scripts/bandeja.py

## 7. Cuando este andando

- Pasar `MODO_ENVIO=automatico` recien despues de unos dias mirando borradores.
- Crear plantillas en espanol en Meta (hoy la unica aprobada es `hello_world`, en
  ingles). Solo hacen falta para ESCRIBIR primero; si el cliente escribe, la ventana
  de 24h deja responder libre.

## 8. Panel de conversaciones

`https://TU-DOMINIO/panel?token=EL_PANEL_TOKEN`

Muestra los chats con el ultimo mensaje y, al entrar a uno, la conversacion completa
entre el cliente y Pampa. Los que pidieron hablar con una persona aparecen primero,
marcados. La lista se refresca sola cada 30 segundos.

El token queda guardado en una cookie por 30 dias, asi que la primera vez se entra con
`?token=...` y despues alcanza con `https://TU-DOMINIO/panel`. Guardalo en favoritos
del celular.

Es de SOLO LECTURA: no envia mensajes ni cambia nada.

Si `PANEL_TOKEN` no esta configurado, `/panel` devuelve 404 a proposito: son
conversaciones de clientes en una URL publica y no puede quedar abierto por olvido.
