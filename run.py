"""Point d'entrée HTTP pour la prod (Render, etc.) : pas de bash, pas de souci de CRLF."""
import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
