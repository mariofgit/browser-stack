.PHONY: help setup setup-agent setup-ui dev agent ui

AGENT_PORT ?= 8012
AGENT_ENV  := agent/.env
UI_ENV     := ui/.env.local

# ── Help ────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  make setup        Primera vez: crea .env, venv Python, npm install"
	@echo "  make dev          Levanta agent + UI en paralelo (dos procesos)"
	@echo "  make agent        Solo el agent Python  (puerto $(AGENT_PORT))"
	@echo "  make ui           Solo el frontend Next.js (puerto 3000)"
	@echo ""

# ── Setup ───────────────────────────────────────────────────────────────────
setup: setup-agent setup-ui
	@echo ""
	@echo "✓ Setup completo. Edita agent/.env y ui/.env.local con tus claves y ejecuta: make dev"

setup-agent:
	@echo "→ Agent: creando .env desde .env.example (si no existe)..."
	@test -f $(AGENT_ENV) || cp agent/.env.example $(AGENT_ENV)
	@echo "→ Agent: creando virtualenv..."
	@cd agent && python3 -m venv .venv
	@echo "→ Agent: instalando dependencias Python..."
	@cd agent && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -e .
	@echo "→ Agent: instalando Chromium para Playwright..."
	@cd agent && .venv/bin/playwright install chromium
	@echo "✓ Agent listo."

setup-ui:
	@echo "→ UI: creando .env.local desde .env.example (si no existe)..."
	@test -f $(UI_ENV) || cp ui/.env.example $(UI_ENV)
	@echo "→ UI: instalando dependencias Node..."
	@cd ui && npm install --silent
	@echo "✓ UI lista."

# ── Dev (ambos servicios) ────────────────────────────────────────────────────
dev:
	@echo "→ Levantando agent (puerto $(AGENT_PORT)) y UI (puerto 3000)…"
	@echo "   Ctrl+C detiene ambos."
	@trap 'kill 0' INT; \
	  (cd agent && .venv/bin/uvicorn runtime.app:app --port $(AGENT_PORT) --reload 2>&1 | sed 's/^/[agent] /') & \
	  (cd ui && npm run dev 2>&1 | sed 's/^/[ui]    /') & \
	  wait

# ── Servicios individuales ───────────────────────────────────────────────────
agent:
	@cd agent && .venv/bin/uvicorn runtime.app:app --port $(AGENT_PORT) --reload

ui:
	@cd ui && npm run dev
