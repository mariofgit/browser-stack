# browser-stack

WSJ Morning Shot standalone: scraping con Browserbase + Playwright, cotizaciones Yahoo Finance y resumen opcional con OpenAI.

## Estructura

```
browser-stack/
  agent/          # Backend Python — FastAPI
    runtime/
      app.py                 # Endpoint POST /wsj-morning-shot + /health
      browserbase_wsj.py     # Navegación cloud con Browserbase + Playwright
    pyproject.toml
    .env.example
  ui/             # Frontend Next.js — panel visual
    src/app/
      page.tsx                         # Panel WSJ morning shot
      api/wsj-morning-shot/route.ts    # Proxy → agent
    .env.example
```

## Setup rápido

```bash
# 1. Primera vez: crea .env, venv Python, npm install y Playwright
make setup

# 2. Edita las claves (obligatorio antes de correr)
#    agent/.env    → BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID (+ OPENAI_API_KEY opcional)
#    ui/.env.local → AGENT_BASE_URL (default http://localhost:8012, normalmente no hace falta cambiarlo)

# 3. Levantar ambos servicios en paralelo
make dev
```

Abre [http://localhost:3000](http://localhost:3000) y pulsa **Run morning shot**.

También puedes levantarlos por separado:

```bash
make agent   # solo FastAPI en :8012
make ui      # solo Next.js en :3000
```

## Variables de entorno clave

| Variable | Descripción |
|---|---|
| `BROWSERBASE_API_KEY` | Clave API de Browserbase (obligatoria para modo cloud) |
| `BROWSERBASE_PROJECT_ID` | Project ID de Browserbase |
| `WSJ_SESSION_COOKIE` | Alternativa legada sin Browserbase (cookie de suscriptor) |
| `OPENAI_API_KEY` | Resumen estructurado OpenAI (opcional) |
| `WSJ_FINANCE_PATH` | Ruta de la sección finance (default `/finance`) |
| `AGENT_BASE_URL` | URL del agent desde la UI (default `http://localhost:8012`) |

Ver `agent/.env.example` y `ui/.env.example` para todas las variables.

## Flujo

1. UI → `POST /api/wsj-morning-shot` (Next.js proxy)
2. Proxy → `POST /wsj-morning-shot` (FastAPI agent, puerto 8012)
3. Agent → Browserbase (Playwright) o httpx + cookie
4. Extracción HTML + Yahoo Finance quotes + OpenAI summary (opcional)
5. JSON de respuesta renderizado en el panel
