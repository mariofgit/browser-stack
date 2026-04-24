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

### 1. Agent (Python)

```bash
cd agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env   # rellenar BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID, etc.
uvicorn runtime.app:app --port 8012 --reload
```

### 2. UI (Next.js)

```bash
cd ui
npm install
cp .env.example .env.local   # AGENT_BASE_URL=http://localhost:8012
npm run dev
```

Abre [http://localhost:3000](http://localhost:3000) y pulsa **Run morning shot**.

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
