#!/usr/bin/env bash
# Démarrage Render : évite toute erreur de frappe sur "app.main:app" (sans espace).
set -e
PORT="${PORT:-8000}"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
