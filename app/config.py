import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

DATABASE_URL = "sqlite+aiosqlite:///" + str(BASE_DIR / "data" / "internal_comms.db")
AVATAR_UPLOAD_DIR = BASE_DIR / "storage" / "avatars"
MAX_AVATAR_BYTES = 3 * 1024 * 1024  # 3 Mo
AVATAR_THUMB_SIZE = 512

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me-in-production-use-openssl-rand")
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", str(60 * 60 * 24 * 7)))
SESSION_HTTPS_ONLY = os.environ.get("SESSION_HTTPS_ONLY", "0").lower() in ("1", "true", "yes")

ALLOW_PUBLIC_REGISTRATION = os.environ.get("ALLOW_PUBLIC_REGISTRATION", "1").lower() in (
    "1",
    "true",
    "yes",
)

RATE_LIMIT_LOGIN_MAX = int(os.environ.get("RATE_LIMIT_LOGIN_MAX", "30"))
RATE_LIMIT_LOGIN_WINDOW_SEC = int(os.environ.get("RATE_LIMIT_LOGIN_WINDOW_SEC", "300"))
RATE_LIMIT_REGISTER_MAX = int(os.environ.get("RATE_LIMIT_REGISTER_MAX", "8"))
RATE_LIMIT_REGISTER_WINDOW_SEC = int(os.environ.get("RATE_LIMIT_REGISTER_WINDOW_SEC", "3600"))

MESSAGE_EDIT_WINDOW_MINUTES = int(os.environ.get("MESSAGE_EDIT_WINDOW_MINUTES", "60"))

# Dernière activité (ping) plus récente que ce délai = considéré « en ligne » pour l’admin
PRESENCE_ONLINE_SECONDS = int(os.environ.get("PRESENCE_ONLINE_SECONDS", "180"))
