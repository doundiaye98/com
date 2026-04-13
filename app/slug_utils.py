from __future__ import annotations

import re
import unicodedata


def is_reserved_dm_slug(name: str) -> bool:
    """Nom réservé aux canaux de messages directs (ex. dm-1-2)."""
    return bool(re.match(r"^dm-\d+-\d+$", name))


def slugify_text(raw: str, max_len: int = 80) -> str:
    s = unicodedata.normalize("NFKD", raw.strip())
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace(" ", "-")
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:max_len]
