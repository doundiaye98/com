from __future__ import annotations

import io
import secrets
from pathlib import Path

from PIL import Image, ImageOps

from app.config import AVATAR_THUMB_SIZE, MAX_AVATAR_BYTES


def validate_and_build_jpeg(file_content: bytes) -> bytes:
    if len(file_content) > MAX_AVATAR_BYTES:
        raise ValueError("Fichier trop volumineux (maximum 3 Mo).")
    try:
        im = Image.open(io.BytesIO(file_content))
        im.load()
    except Exception:
        raise ValueError("Le fichier n'est pas une image valide.") from None
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    im.thumbnail((AVATAR_THUMB_SIZE, AVATAR_THUMB_SIZE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()


def new_avatar_filename(user_id: int) -> str:
    return f"u{user_id}_{secrets.token_hex(8)}.jpg"


def save_user_avatar(upload_dir: Path, user_id: int, jpeg_bytes: bytes) -> str:
    upload_dir.mkdir(parents=True, exist_ok=True)
    name = new_avatar_filename(user_id)
    (upload_dir / name).write_bytes(jpeg_bytes)
    return name


def avatar_public_url(filename: str | None) -> str | None:
    if filename:
        return f"/media/avatars/{filename}"
    return None


def remove_avatar_file(upload_dir: Path, filename: str | None) -> None:
    if not filename:
        return
    path = upload_dir / filename
    if path.is_file():
        path.unlink()
