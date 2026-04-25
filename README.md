# browser-stack — Finance Agent

Chat AI-native para mercados financieros: morning shot (WSJ) y resumen de noticias.

La plantilla y el JSON del resumen de noticias se documentan en [docs/RESUMEN_NOTICIAS_ESTRUCTURA.md](docs/RESUMEN_NOTICIAS_ESTRUCTURA.md) (alineada al PDF de referencia BIVA).

## Estructura

```
browser-stack/
  agent/                    # Backend Python — FastAPI
    runtime/
      app.py                # POST /chat · /morning-shot · /wsj-morning-shot · /health
      browserbase_wsj.py    # Navegación cloud con Browserbase + Playwright
    Dockerfile              # Para Railway (producción)
    railway.toml
    pyproject.toml
    .env.example
  ui/                       # Frontend Next.js — chat UI
    src/app/
      page.tsx              # ChatShell (interfaz de chat)
      api/
        chat/route.ts       # Proxy → agent POST /chat   (principal)
        morning-shot/route.ts
        news-summary/route.ts
        wsj-morning-shot/route.ts   # alias legacy
    vercel.json             # Para Vercel (producción)
    .env.example
  Makefile
```

## Setup rápido (local)

```bash
# 1. Primera vez: crea .env, venv Python, npm install y Playwright
make setup

# 2. Edita las claves (obligatorio antes de correr)
#    agent/.env    → BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID, OPENAI_API_KEY
#    ui/.env.local → AGENT_BASE_URL (default http://localhost:8012)

# 3. Levantar ambos servicios en paralelo
make dev
```

Abre [http://localhost:3000](http://localhost:3000) y escribe en el chat:
- `"dame el morning shot"` — genera el morning shot de WSJ
- `"morning shot de wsj"` — ídem, especificando fuente
- `"resumen de noticias"` — próximamente
- `"cotización de AAPL"` — precio de cualquier ticker via Yahoo Finance

## Variables de entorno clave

| Variable | Descripción |
|---|---|
| `BROWSERBASE_API_KEY` | Clave API de Browserbase (obligatoria para scraping) |
| `BROWSERBASE_PROJECT_ID` | Project ID de Browserbase |
| `OPENAI_API_KEY` | Resumen estructurado OpenAI (opcional) |
| `AGENT_BASE_URL` | URL del agent desde la UI (default `http://localhost:8012`) |
| `WSJ_SESSION_COOKIE` | Alternativa sin Browserbase (cookie de suscriptor WSJ) |

Ver `agent/.env.example` para todas las variables.

## Flujo del chat

```
Usuario escribe → POST /api/chat (Next.js proxy)
  → POST /chat (FastAPI, puerto 8012)
    → _dispatch_intent: keywords → herramienta
    → morning_shot: Browserbase + Yahoo Finance + OpenAI
    → news_summary: httpx fuentes públicas + OpenAI (próximo)
    → quote: yfinance directo
  → JSON estructurado renderizado en burbuja del chat
```

Si el scraping encuentra un paywall, el agente muestra un iframe de Live View de Browserbase dentro del chat. El usuario se autentica y presiona **"Ya me autentiqué →"** para continuar.

## Deploy en producción

### Backend → Railway

1. Crea un nuevo proyecto en Railway apuntando a `browser-stack/agent/`
2. Railway detecta el `Dockerfile` automáticamente
3. Agrega las variables de entorno en el dashboard de Railway
4. Railway expone una URL pública (ej. `https://finance-agent-xxx.railway.app`)

### Frontend → Vercel

1. Conecta el repo en Vercel, carpeta raíz `browser-stack/ui/`
2. Agrega `AGENT_BASE_URL=https://finance-agent-xxx.railway.app` en Vercel
3. Deploy automático en cada `git push main`
