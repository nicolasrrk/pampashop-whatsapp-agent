Lee el archivo CLAUDE.md completo. Contiene todas las instrucciones detalladas.

Ejecuta el flujo de onboarding de AgentKit siguiendo las 5 fases EN ORDEN:

FASE 1 — Bienvenida y verificación del entorno
- Muestra el mensaje de bienvenida
- Verifica Python >= 3.11
- Crea las carpetas necesarias (agent/, agent/providers/, config/, knowledge/, tests/)
- Genera requirements.txt e instala dependencias
- Crea .env base

FASE 2 — Entrevista del negocio
- Haz las 10 preguntas UNA POR UNA
- Espera respuesta antes de continuar a la siguiente
- PREGUNTA 9: el usuario elige su proveedor de WhatsApp (Zernio o Meta Cloud API directo)
- PREGUNTA 10: pide las credenciales específicas del proveedor elegido
  - Zernio: ZERNIO_API_KEY + ZERNIO_WEBHOOK_SECRET
  - Meta:   META_ACCESS_TOKEN + META_PHONE_NUMBER_ID + META_VERIFY_TOKEN + META_APP_SECRET
- Si no tiene número de WhatsApp todavía, ofrécele el sandbox de Zernio
- Guarda todas las respuestas para la Fase 3

FASE 3 — Generación del agente
- Genera config/business.yaml con datos del negocio
- Genera config/prompts.yaml con system prompt poderoso y específico
- Si hay archivos en /knowledge, léelos e incorpóralos al prompt
- Genera agent/providers/ con el proveedor elegido (base.py + __init__.py + adaptador)
- Genera agent/main.py (FastAPI + webhook, firma verificada, BackgroundTasks, dedup)
- Genera agent/brain.py (Claude API)
- Genera agent/memory.py (historial + tabla de eventos procesados)
- Genera agent/tools.py (herramientas según caso de uso)
- Genera tests/test_local.py (simulador de chat)
- Genera Dockerfile, docker-compose.yml y .dockerignore
- Configura .env con WHATSAPP_PROVIDER y las API keys del usuario

FASE 4 — Testing local
- Ejecuta python tests/test_local.py
- El usuario chatea con su agente en la terminal
- Verifica que el servidor arranca y que GET / responde status ok
- Si hay ajustes, modifica prompts.yaml y repite
- No avanza sin aprobación del usuario

FASE 5 — Deploy a Railway
- Solo si el usuario quiere
- Build Docker + instrucciones de Railway
- Configuración de webhook específica para el proveedor elegido
- Explicarle la ventana de 24 horas de WhatsApp

REGLAS:
- Habla siempre en español
- Una pregunta a la vez
- Nunca hardcodees API keys
- No avances de fase sin confirmación
- El agente debe funcionar antes de hablar de deploy
- Genera SOLO el adaptador del proveedor elegido, no los dos
- No cambies el modelo de Claude por tu cuenta para ahorrar: es decisión del usuario
