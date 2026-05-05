from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.middleware.sessions import SessionMiddleware

from app.auth_utils import hash_password, verify_password
from app.avatar_utils import (
    avatar_public_url,
    remove_avatar_file,
    save_user_avatar,
    validate_and_build_jpeg,
)
from app.config import (
    ALLOW_PUBLIC_REGISTRATION,
    AVATAR_UPLOAD_DIR,
    BASE_DIR,
    MESSAGE_EDIT_WINDOW_MINUTES,
    PRESENCE_ONLINE_SECONDS,
    RATE_LIMIT_LOGIN_MAX,
    RATE_LIMIT_LOGIN_WINDOW_SEC,
    RATE_LIMIT_REGISTER_MAX,
    RATE_LIMIT_REGISTER_WINDOW_SEC,
    SECRET_KEY,
    SESSION_HTTPS_ONLY,
    SESSION_MAX_AGE,
)
from app.channel_access import KIND_DIRECT, channel_kind, ensure_channel_access
from app.db import SessionLocal, get_session, init_db
from app.deps import get_current_user, require_admin
from app.dm_service import get_or_create_dm_channel
from app.group_service import (
    channels_for_group,
    ensure_chat_group_membership,
    ensure_default_channel_for_empty_groups,
    list_chat_groups_for_user,
    list_global_standard_channels,
    unique_chat_group_slug,
    user_can_manage_chat_group,
)
from app.models import Channel, ChatGroup, ChatGroupMember, Message, User
from app.password_policy import validate_password_for_user
from app.rate_limit import SlidingWindowLimiter, client_ip
from app.schemas import (
    ChannelCreate,
    ChannelOut,
    ChatGroupCreate,
    ChatGroupMemberAdd,
    ChatGroupOut,
    ChatSidebarOut,
    DmConversationOut,
    DmOpenRequest,
    GroupWithChannelsOut,
    MessageBroadcast,
    MessageDeletedBroadcast,
    MessageOut,
    MessageUpdate,
    ProfileUpdate,
    RegisterRequest,
    UserCreate,
    UserMini,
    UserOut,
)
from app.security_middleware import SecurityHeadersMiddleware
from app.seed import ensure_seed_data
from app.slug_utils import is_reserved_dm_slug, slugify_text
from app.ws_manager import hub
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
WS_TOKEN_SALT = "internal-comms-ws"
MENTION_RE = re.compile(r"@([A-Za-z0-9._-]{2,120})")
MESSAGE_UPLOAD_DIR = BASE_DIR / "storage" / "messages"
MAX_MESSAGE_FILE_BYTES = 25 * 1024 * 1024
ALLOWED_MESSAGE_MIME_PREFIXES = ("image/", "video/")
ALLOWED_MESSAGE_MIME_EXACT = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "application/zip",
    "application/x-zip-compressed",
}
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

login_limiter = SlidingWindowLimiter(RATE_LIMIT_LOGIN_MAX, RATE_LIMIT_LOGIN_WINDOW_SEC)
register_limiter = SlidingWindowLimiter(RATE_LIMIT_REGISTER_MAX, RATE_LIMIT_REGISTER_WINDOW_SEC)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _format_dt_admin(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return _as_utc(dt).strftime("%d/%m/%Y %H:%M UTC")


def _presence_online(last_seen: datetime | None, now: datetime, threshold_sec: int) -> bool:
    """SQLite renvoie souvent des datetimes naïfs : comparer en UTC explicite."""
    if not last_seen:
        return False
    return (_as_utc(now) - _as_utc(last_seen)).total_seconds() < float(threshold_sec)


templates.env.filters["dt_admin"] = _format_dt_admin
templates.env.filters["presence_online"] = _presence_online


def slugify_channel_name(name: str) -> str:
    slug = slugify_text(name, 80)
    if not slug:
        raise HTTPException(status_code=400, detail="Nom de canal invalide")
    if is_reserved_dm_slug(slug):
        raise HTTPException(status_code=400, detail="Ce nom est réservé pour le système")
    return slug


def _parse_mentions(body: str, users: list[User]) -> list[int]:
    if not body:
        return []
    wanted = {m.group(1).strip().lower() for m in MENTION_RE.finditer(body)}
    if not wanted:
        return []
    out: list[int] = []
    for u in users:
        key = u.display_name.strip().lower()
        if key in wanted:
            out.append(u.id)
    return sorted(set(out))


def _is_allowed_message_mime(content_type: str | None) -> bool:
    if not content_type:
        return False
    ct = content_type.lower().strip()
    if any(ct.startswith(prefix) for prefix in ALLOWED_MESSAGE_MIME_PREFIXES):
        return True
    return ct in ALLOWED_MESSAGE_MIME_EXACT


async def _save_message_file(file: UploadFile) -> tuple[str, str, str, int]:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Fichier vide.")
    if len(content) > MAX_MESSAGE_FILE_BYTES:
        raise HTTPException(status_code=400, detail="Fichier trop volumineux (max 25 Mo).")
    if not _is_allowed_message_mime(file.content_type):
        raise HTTPException(
            status_code=400,
            detail="Type de fichier non supporté (images, vidéos et documents uniquement).",
        )
    original = (file.filename or "fichier").strip()[:255]
    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", original).strip() or "fichier"
    ext = Path(safe_name).suffix[:12]
    stored = f"{secrets.token_hex(12)}{ext}"
    target = MESSAGE_UPLOAD_DIR / stored
    target.write_bytes(content)
    return (f"/media/messages/{stored}", safe_name, (file.content_type or "application/octet-stream"), len(content))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with SessionLocal() as session:
        await ensure_seed_data(session)
        await ensure_default_channel_for_empty_groups(session)
    yield


app = FastAPI(title="Communication interne", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=SESSION_HTTPS_ONLY,
)
app.add_middleware(SecurityHeadersMiddleware)

static_dir = BASE_DIR / "app" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

AVATAR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media/avatars", StaticFiles(directory=str(AVATAR_UPLOAD_DIR)), name="avatars")
MESSAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media/messages", StaticFiles(directory=str(MESSAGE_UPLOAD_DIR)), name="message-files")


def build_user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        is_admin=u.is_admin,
        is_active=u.is_active,
        created_at=u.created_at,
        avatar_url=avatar_public_url(u.avatar_filename),
    )


def channel_user_can_delete(user: User, ch: Channel) -> bool:
    if user.is_admin:
        return True
    if channel_kind(ch) == KIND_DIRECT:
        return False
    if ch.name == "general":
        return False
    if ch.created_by_id is not None and ch.created_by_id == user.id:
        return True
    cg = ch.chat_group
    if cg is not None and cg.created_by_id is not None and cg.created_by_id == user.id:
        return True
    return False


def build_channel_out(user: User, ch: Channel) -> ChannelOut:
    label = ch.name
    group_name = None
    gcb = None
    cg = ch.chat_group
    if cg is not None:
        group_name = cg.name
        gcb = cg.created_by_id
        prefix = cg.slug + "-"
        if ch.name.startswith(prefix):
            label = ch.name[len(prefix) :]
    return ChannelOut(
        id=ch.id,
        name=ch.name,
        display_label=label,
        topic=ch.topic,
        is_private=ch.is_private,
        created_by_id=ch.created_by_id,
        kind=ch.kind or "standard",
        created_at=ch.created_at,
        group_id=ch.group_id,
        group_name=group_name,
        group_created_by_id=gcb,
        can_delete=channel_user_can_delete(user, ch),
    )


async def build_chat_sidebar(session: AsyncSession, user: User) -> ChatSidebarOut:
    globals_ = await list_global_standard_channels(session)
    groups_db = await list_chat_groups_for_user(session, user)
    groups_out: list[GroupWithChannelsOut] = []
    for g in groups_db:
        chs = await channels_for_group(session, g.id)
        groups_out.append(
            GroupWithChannelsOut(
                group=ChatGroupOut.model_validate(g),
                channels=[build_channel_out(user, c) for c in chs],
                can_manage=user_can_manage_chat_group(user, g),
            )
        )
    return ChatSidebarOut(
        global_channels=[build_channel_out(user, c) for c in globals_],
        groups=groups_out,
    )


def message_to_out(m: Message) -> MessageOut:
    author_name = m.author.display_name if m.author else "?"
    av = None
    if m.author:
        av = avatar_public_url(m.author.avatar_filename)
    mentions: list[int] = []
    if m.mention_user_ids:
        try:
            raw = json.loads(m.mention_user_ids)
            if isinstance(raw, list):
                mentions = [int(x) for x in raw if isinstance(x, int) or (isinstance(x, str) and x.isdigit())]
        except (ValueError, TypeError):
            mentions = []
    return MessageOut(
        id=m.id,
        channel_id=m.channel_id,
        user_id=m.user_id,
        author_name=author_name,
        author_avatar_url=av,
        body=m.body,
        mentions=mentions,
        attachment_url=m.attachment_url,
        attachment_name=m.attachment_name,
        attachment_mime=m.attachment_mime,
        attachment_size=m.attachment_size,
        created_at=m.created_at,
        edited_at=m.edited_at,
    )


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/chat", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, notice: str | None = None):
    if request.session.get("user_id"):
        return RedirectResponse("/chat", status_code=302)
    notice_msg = None
    if notice == "register_off":
        notice_msg = (
            "Les inscriptions publiques sont fermées. "
            "Demandez à un administrateur de créer votre compte."
        )
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "error": None,
            "notice": notice_msg,
            "allow_registration": ALLOW_PUBLIC_REGISTRATION,
        },
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    email: str = Form(...),
    password: str = Form(...),
):
    if not login_limiter.allow(client_ip(request)):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "error": "Trop de tentatives de connexion. Réessayez dans quelques minutes.",
                "notice": None,
                "allow_registration": ALLOW_PUBLIC_REGISTRATION,
            },
            status_code=429,
        )
    result = await session.execute(select(User).where(User.email == email.strip().lower()))
    user = result.scalar_one_or_none()
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "error": "E-mail ou mot de passe incorrect.",
                "notice": None,
                "allow_registration": ALLOW_PUBLIC_REGISTRATION,
            },
            status_code=401,
        )
    now = _utcnow()
    user.last_login_at = now
    user.last_seen_at = now
    await session.commit()
    request.session["user_id"] = user.id
    return RedirectResponse("/chat", status_code=302)


@app.get("/logout")
async def logout(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    uid = request.session.get("user_id")
    if uid:
        r = await session.execute(select(User).where(User.id == int(uid)))
        u = r.scalar_one_or_none()
        if u:
            u.last_logout_at = _utcnow()
            await session.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/chat", status_code=302)
    if not ALLOW_PUBLIC_REGISTRATION:
        return RedirectResponse("/login?notice=register_off", status_code=302)
    return templates.TemplateResponse(
        request,
        "register.html",
        {"request": request, "error": None},
    )


@app.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    email: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
):
    if not ALLOW_PUBLIC_REGISTRATION:
        return RedirectResponse("/login?notice=register_off", status_code=302)
    if not register_limiter.allow(client_ip(request)):
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "request": request,
                "error": "Trop de créations de compte depuis cette adresse. Réessayez plus tard.",
            },
            status_code=429,
        )
    email_norm = email.strip().lower()
    exists = await session.execute(select(User).where(User.email == email_norm))
    if exists.scalar_one_or_none():
        return templates.TemplateResponse(
            request,
            "register.html",
            {"request": request, "error": "Cette adresse e-mail est déjà utilisée."},
            status_code=409,
        )
    pwd_err = validate_password_for_user(password)
    if pwd_err:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"request": request, "error": pwd_err},
            status_code=400,
        )
    u = User(
        email=email_norm,
        display_name=display_name.strip(),
        password_hash=hash_password(password),
        is_admin=False,
        is_active=True,
    )
    session.add(u)
    await session.commit()
    await session.refresh(u)
    now = _utcnow()
    u.last_login_at = now
    u.last_seen_at = now
    await session.commit()
    request.session["user_id"] = u.id
    return RedirectResponse("/chat", status_code=302)


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
):
    return templates.TemplateResponse(
        request,
        "profile.html",
        {"request": request, "user": user},
    )


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    sidebar = await build_chat_sidebar(session, user)
    order_ids = [c.id for c in sidebar.global_channels] + [
        c.id for g in sidebar.groups for c in g.channels
    ]
    active_id = order_ids[0] if order_ids else None
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "request": request,
            "user": user,
            "sidebar_initial": sidebar.model_dump(mode="json"),
            "active_channel_id": active_id,
            "message_edit_minutes": MESSAGE_EDIT_WINDOW_MINUTES,
        },
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
):
    uq = await session.execute(select(User).order_by(User.created_at.desc()))
    cq = await session.execute(
        select(Channel)
        .where(or_(Channel.kind == "standard", Channel.kind.is_(None)))
        .options(selectinload(Channel.chat_group))
        .order_by(Channel.name)
    )
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "request": request,
            "user": admin,
            "users": list(uq.scalars().all()),
            "channels": list(cq.scalars().all()),
            "presence_now": _utcnow(),
            "presence_online_seconds": PRESENCE_ONLINE_SECONDS,
        },
    )


# --- API ---


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/me", response_model=UserOut)
async def api_me(user: Annotated[User, Depends(get_current_user)]):
    return build_user_out(user)


@app.post("/api/me/ping")
async def api_me_ping(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Enregistre une activité récente (utilisé par le chat / admin pour la présence)."""
    user.last_seen_at = _utcnow()
    await session.commit()
    return {"ok": True}


@app.post("/api/register", response_model=UserOut, status_code=201)
async def api_register(
    request: Request,
    body: RegisterRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    if not ALLOW_PUBLIC_REGISTRATION:
        raise HTTPException(status_code=403, detail="Inscription publique désactivée")
    if not register_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="Trop de demandes. Réessayez plus tard.")
    pwd_err = validate_password_for_user(body.password)
    if pwd_err:
        raise HTTPException(status_code=400, detail=pwd_err)
    email_norm = body.email.strip().lower()
    exists = await session.execute(select(User).where(User.email == email_norm))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Cet e-mail est déjà utilisé")
    u = User(
        email=email_norm,
        display_name=body.display_name.strip(),
        password_hash=hash_password(body.password),
        is_admin=False,
        is_active=True,
    )
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return build_user_out(u)


@app.patch("/api/profile", response_model=UserOut)
async def api_update_profile(
    body: ProfileUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    if body.display_name is None and body.new_password is None:
        raise HTTPException(status_code=400, detail="Aucune modification demandée")
    if body.display_name is not None:
        user.display_name = body.display_name.strip()
    if body.new_password:
        pwd_err = validate_password_for_user(body.new_password)
        if pwd_err:
            raise HTTPException(status_code=400, detail=pwd_err)
        user.password_hash = hash_password(body.new_password)
    await session.commit()
    await session.refresh(user)
    return build_user_out(user)


@app.post("/api/profile/avatar", response_model=UserOut)
async def api_upload_avatar(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    file: UploadFile = File(...),
):
    if file.content_type and file.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(status_code=400, detail="Formats acceptés : JPEG, PNG ou WebP.")
    content = await file.read()
    try:
        jpeg = validate_and_build_jpeg(content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    old = user.avatar_filename
    fn = save_user_avatar(AVATAR_UPLOAD_DIR, user.id, jpeg)
    user.avatar_filename = fn
    await session.commit()
    await session.refresh(user)
    remove_avatar_file(AVATAR_UPLOAD_DIR, old)
    return build_user_out(user)


@app.delete("/api/profile/avatar", response_model=UserOut)
async def api_delete_avatar(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    old = user.avatar_filename
    user.avatar_filename = None
    await session.commit()
    await session.refresh(user)
    remove_avatar_file(AVATAR_UPLOAD_DIR, old)
    return build_user_out(user)


@app.get("/api/ws-token")
async def api_ws_token(user: Annotated[User, Depends(get_current_user)]):
    ser = URLSafeTimedSerializer(SECRET_KEY, salt=WS_TOKEN_SALT)
    return {"token": ser.dumps({"uid": user.id})}


@app.get("/api/chat-sidebar", response_model=ChatSidebarOut)
async def api_chat_sidebar(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    return await build_chat_sidebar(session, user)


@app.get("/api/channels", response_model=list[ChannelOut])
async def list_channels(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Liste plate de tous les canaux standards visibles (globaux + groupes)."""
    sidebar = await build_chat_sidebar(session, user)
    return sidebar.global_channels + [c for g in sidebar.groups for c in g.channels]


@app.post("/api/groups", response_model=ChatGroupOut, status_code=201)
async def create_chat_group(
    body: ChatGroupCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    base = slugify_text(body.name.strip(), 80)
    if not base:
        raise HTTPException(status_code=400, detail="Nom de groupe invalide")
    slug = await unique_chat_group_slug(session, base)
    g = ChatGroup(
        name=body.name.strip()[:120],
        slug=slug,
        description=(body.description.strip() if body.description else None),
        created_by_id=user.id,
    )
    session.add(g)
    await session.flush()
    session.add(ChatGroupMember(group_id=g.id, user_id=user.id))
    # Canal d’accès par défaut : sans canal, personne ne peut « entrer » dans le groupe depuis la barre latérale.
    acc_suffix = "-accueil"
    acc_base_max = 80 - len(acc_suffix)
    acc_name = (slug[:acc_base_max] + acc_suffix)[:80]
    if is_reserved_dm_slug(acc_name):
        acc_name = (slug[: max(1, acc_base_max - 2)] + "-acc")[:80]
    session.add(
        Channel(
            name=acc_name,
            topic="Canal principal du groupe — bienvenue !",
            is_private=False,
            created_by_id=user.id,
            group_id=g.id,
        )
    )
    await session.commit()
    await session.refresh(g)
    return ChatGroupOut.model_validate(g)


@app.post("/api/groups/{group_id}/members", status_code=204)
async def add_chat_group_member(
    group_id: int,
    body: ChatGroupMemberAdd,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    gr = await session.get(ChatGroup, group_id)
    if not gr:
        raise HTTPException(status_code=404, detail="Groupe introuvable")
    if not user_can_manage_chat_group(user, gr):
        raise HTTPException(status_code=403, detail="Vous ne pouvez pas gérer ce groupe")
    ur = await session.execute(select(User).where(User.id == body.user_id, User.is_active.is_(True)))
    if ur.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    ex = await session.execute(
        select(ChatGroupMember).where(
            ChatGroupMember.group_id == group_id,
            ChatGroupMember.user_id == body.user_id,
        )
    )
    if ex.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Cette personne est déjà dans le groupe")
    session.add(ChatGroupMember(group_id=group_id, user_id=body.user_id))
    await session.commit()


@app.delete("/api/groups/{group_id}/members/{member_user_id}", status_code=204)
async def remove_chat_group_member(
    group_id: int,
    member_user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    gr = await session.get(ChatGroup, group_id)
    if not gr:
        raise HTTPException(status_code=404, detail="Groupe introuvable")
    if member_user_id != user.id and not user_can_manage_chat_group(user, gr):
        raise HTTPException(status_code=403, detail="Action non autorisée")
    r = await session.execute(
        select(ChatGroupMember).where(
            ChatGroupMember.group_id == group_id,
            ChatGroupMember.user_id == member_user_id,
        )
    )
    row = r.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404)
    await session.delete(row)
    await session.commit()
    n = await session.scalar(
        select(func.count()).select_from(ChatGroupMember).where(ChatGroupMember.group_id == group_id)
    )
    if n == 0:
        gr2 = await session.get(ChatGroup, group_id)
        if gr2:
            await session.delete(gr2)
            await session.commit()


@app.delete("/api/groups/{group_id}", status_code=204)
async def delete_chat_group(
    group_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    gr = await session.get(ChatGroup, group_id)
    if not gr:
        raise HTTPException(status_code=404)
    if not user_can_manage_chat_group(user, gr):
        raise HTTPException(status_code=403, detail="Suppression non autorisée")
    await session.delete(gr)
    await session.commit()


@app.get("/api/users/for-dm", response_model=list[UserMini])
async def list_users_for_dm(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    r = await session.execute(
        select(User)
        .where(User.is_active.is_(True), User.id != user.id)
        .order_by(User.display_name)
    )
    return [
        UserMini(
            id=u.id,
            display_name=u.display_name,
            avatar_url=avatar_public_url(u.avatar_filename),
        )
        for u in r.scalars().all()
    ]


@app.get("/api/dm/conversations", response_model=list[DmConversationOut])
async def dm_conversation_list(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    r = await session.execute(
        select(Channel)
        .where(
            Channel.kind == KIND_DIRECT,
            or_(Channel.dm_user_low_id == user.id, Channel.dm_user_high_id == user.id),
        )
        .order_by(Channel.last_activity_at.desc().nulls_last(), Channel.id.desc())
    )
    out: list[DmConversationOut] = []
    for ch in r.scalars().all():
        if ch.dm_user_low_id is None or ch.dm_user_high_id is None:
            continue
        peer_id = ch.dm_user_high_id if ch.dm_user_low_id == user.id else ch.dm_user_low_id
        ur = await session.execute(select(User).where(User.id == peer_id))
        peer = ur.scalar_one_or_none()
        if not peer or not peer.is_active:
            continue
        out.append(
            DmConversationOut(
                channel_id=ch.id,
                peer_id=peer.id,
                peer_display_name=peer.display_name,
                peer_avatar_url=avatar_public_url(peer.avatar_filename),
                last_activity_at=ch.last_activity_at,
            )
        )
    return out


@app.post("/api/dm/open", response_model=DmConversationOut)
async def dm_open_or_create(
    body: DmOpenRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    if body.peer_user_id == user.id:
        raise HTTPException(status_code=400, detail="Vous ne pouvez pas ouvrir une conversation avec vous-même")
    pr = await session.execute(select(User).where(User.id == body.peer_user_id, User.is_active.is_(True)))
    peer = pr.scalar_one_or_none()
    if not peer:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable ou inactif")
    try:
        ch = await get_or_create_dm_channel(session, user.id, peer.id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Conversation invalide") from None
    return DmConversationOut(
        channel_id=ch.id,
        peer_id=peer.id,
        peer_display_name=peer.display_name,
        peer_avatar_url=avatar_public_url(peer.avatar_filename),
        last_activity_at=ch.last_activity_at,
    )


@app.post("/api/channels", response_model=ChannelOut)
async def create_channel(
    body: ChannelCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    if body.group_id is None:
        slug = slugify_channel_name(body.name)
        if slug == "general":
            raise HTTPException(status_code=400, detail="Le nom « general » est réservé")
        exists = await session.execute(select(Channel).where(Channel.name == slug))
        if exists.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Ce canal existe déjà")
        ch = Channel(
            name=slug,
            topic=body.topic,
            is_private=body.is_private,
            created_by_id=user.id,
            group_id=None,
        )
    else:
        gr = await session.get(ChatGroup, body.group_id)
        if not gr:
            raise HTTPException(status_code=404, detail="Groupe introuvable")
        if not user.is_admin and not await ensure_chat_group_membership(session, user.id, body.group_id):
            raise HTTPException(status_code=403, detail="Vous n’êtes pas membre de ce groupe")
        base = slugify_channel_name(body.name)
        prefix = gr.slug + "-"
        room = max(1, 80 - len(prefix))
        full_slug = (prefix + base[:room])[:80]
        if is_reserved_dm_slug(full_slug):
            raise HTTPException(
                status_code=400,
                detail="Ce nom entre en conflit avec une conversation privée réservée",
            )
        exists = await session.execute(select(Channel).where(Channel.name == full_slug))
        if exists.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Ce canal existe déjà dans ce groupe")
        ch = Channel(
            name=full_slug,
            topic=body.topic,
            is_private=body.is_private,
            created_by_id=user.id,
            group_id=body.group_id,
        )
    session.add(ch)
    await session.commit()
    r = await session.execute(
        select(Channel).options(selectinload(Channel.chat_group)).where(Channel.id == ch.id)
    )
    ch2 = r.scalar_one()
    return build_channel_out(user, ch2)


@app.delete("/api/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    r = await session.execute(
        select(Channel).options(selectinload(Channel.chat_group)).where(Channel.id == channel_id)
    )
    ch = r.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404)
    if ch.kind == KIND_DIRECT:
        if not user.is_admin and user.id not in (ch.dm_user_low_id, ch.dm_user_high_id):
            raise HTTPException(status_code=403, detail="Suppression non autorisée")
    elif ch.name == "general":
        raise HTTPException(status_code=400, detail="Le canal « general » ne peut pas être supprimé")
    elif not channel_user_can_delete(user, ch):
        raise HTTPException(status_code=403, detail="Suppression non autorisée")
    await session.delete(ch)
    await session.commit()


@app.get("/api/channels/{channel_id}/messages", response_model=list[MessageOut])
async def list_messages(
    channel_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = 80,
    q: str | None = None,
):
    cr = await session.execute(
        select(Channel).options(selectinload(Channel.chat_group)).where(Channel.id == channel_id)
    )
    ch = cr.scalar_one_or_none()
    if ch is None:
        raise HTTPException(status_code=404)
    await ensure_channel_access(session, user, ch)
    stmt = (
        select(Message)
        .options(selectinload(Message.author))
        .where(Message.channel_id == channel_id)
    )
    if q and len(q.strip()) >= 2:
        stmt = stmt.where(Message.body.contains(q.strip()))
    mq = await session.execute(
        stmt.order_by(Message.created_at.desc()).limit(min(limit, 200))
    )
    rows = list(mq.scalars().all())
    rows.reverse()
    return [message_to_out(m) for m in rows]


@app.post("/api/channels/{channel_id}/messages", response_model=MessageOut)
async def post_message(
    channel_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    body: str = Form(""),
    file: UploadFile | None = File(default=None),
):
    cr = await session.execute(
        select(Channel).options(selectinload(Channel.chat_group)).where(Channel.id == channel_id)
    )
    ch = cr.scalar_one_or_none()
    if ch is None:
        raise HTTPException(status_code=404)
    await ensure_channel_access(session, user, ch)
    text = body.strip()
    if not text and file is None:
        raise HTTPException(status_code=400, detail="Message vide.")
    mention_ids: list[int] = []
    if text:
        users_r = await session.execute(select(User).where(User.is_active.is_(True)))
        mention_ids = _parse_mentions(text, list(users_r.scalars().all()))
    attachment_url = None
    attachment_name = None
    attachment_mime = None
    attachment_size = None
    if file is not None:
        attachment_url, attachment_name, attachment_mime, attachment_size = await _save_message_file(file)
    msg = Message(
        channel_id=channel_id,
        user_id=user.id,
        body=text,
        mention_user_ids=(json.dumps(mention_ids) if mention_ids else None),
        attachment_url=attachment_url,
        attachment_name=attachment_name,
        attachment_mime=attachment_mime,
        attachment_size=attachment_size,
    )
    session.add(msg)
    ch.last_activity_at = _utcnow()
    await session.commit()
    await session.refresh(msg)
    out = MessageOut(
        id=msg.id,
        channel_id=msg.channel_id,
        user_id=msg.user_id,
        author_name=user.display_name,
        author_avatar_url=avatar_public_url(user.avatar_filename),
        body=msg.body,
        mentions=mention_ids,
        attachment_url=attachment_url,
        attachment_name=attachment_name,
        attachment_mime=attachment_mime,
        attachment_size=attachment_size,
        created_at=msg.created_at,
        edited_at=None,
    )
    await hub.broadcast(channel_id, MessageBroadcast(message=out).model_dump(mode="json"))
    return out


@app.patch("/api/messages/{message_id}", response_model=MessageOut)
async def patch_message(
    message_id: int,
    body: MessageUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    r = await session.execute(
        select(Message)
        .options(selectinload(Message.channel).selectinload(Channel.chat_group))
        .where(Message.id == message_id)
    )
    msg = r.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404)
    await ensure_channel_access(session, user, msg.channel)
    if msg.user_id != user.id:
        raise HTTPException(status_code=403, detail="Vous ne pouvez modifier que vos propres messages")
    created = _as_utc(msg.created_at)
    if _utcnow() - created > timedelta(minutes=MESSAGE_EDIT_WINDOW_MINUTES):
        raise HTTPException(
            status_code=400,
            detail=f"Modification impossible après {MESSAGE_EDIT_WINDOW_MINUTES} minutes.",
        )
    clean_body = body.body.strip()
    users_r = await session.execute(select(User).where(User.is_active.is_(True)))
    mention_ids = _parse_mentions(clean_body, list(users_r.scalars().all()))
    msg.body = clean_body
    msg.mention_user_ids = json.dumps(mention_ids) if mention_ids else None
    msg.edited_at = _utcnow()
    await session.commit()
    r2 = await session.execute(
        select(Message)
        .options(selectinload(Message.author))
        .where(Message.id == message_id)
    )
    msg2 = r2.scalar_one()
    out = message_to_out(msg2)
    await hub.broadcast(msg2.channel_id, MessageBroadcast(message=out).model_dump(mode="json"))
    return out


@app.delete("/api/messages/{message_id}", status_code=204)
async def delete_message(
    message_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
):
    r = await session.execute(
        select(Message)
        .options(selectinload(Message.channel).selectinload(Channel.chat_group))
        .where(Message.id == message_id)
    )
    msg = r.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404)
    await ensure_channel_access(session, user, msg.channel)
    if msg.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="Suppression non autorisée")
    cid = msg.channel_id
    await session.delete(msg)
    await session.commit()
    await hub.broadcast(
        cid,
        MessageDeletedBroadcast(channel_id=cid, message_id=message_id).model_dump(mode="json"),
    )


@app.post("/api/admin/users", response_model=UserOut)
async def admin_create_user(
    body: UserCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
):
    pwd_err = validate_password_for_user(body.password)
    if pwd_err:
        raise HTTPException(status_code=400, detail=pwd_err)
    email = body.email.strip().lower()
    exists = await session.execute(select(User).where(User.email == email))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Cet e-mail est déjà utilisé")
    u = User(
        email=email,
        display_name=body.display_name.strip(),
        password_hash=hash_password(body.password),
        is_admin=body.is_admin,
        is_active=True,
    )
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return build_user_out(u)


@app.patch("/api/admin/users/{user_id}/toggle-active", response_model=UserOut)
async def admin_toggle_active(
    user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Vous ne pouvez pas désactiver votre propre compte")
    r = await session.execute(select(User).where(User.id == user_id))
    u = r.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404)
    u.is_active = not u.is_active
    await session.commit()
    await session.refresh(u)
    return build_user_out(u)


@app.patch("/api/admin/users/{user_id}/toggle-admin", response_model=UserOut)
async def admin_toggle_admin(
    user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Impossible de modifier votre propre rôle ici")
    r = await session.execute(select(User).where(User.id == user_id))
    u = r.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404)
    new_admin = not u.is_admin
    if u.is_admin and not new_admin:
        n = await session.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True)))
        if n is not None and n <= 1:
            raise HTTPException(
                status_code=400,
                detail="Impossible de retirer le dernier administrateur",
            )
    u.is_admin = new_admin
    await session.commit()
    await session.refresh(u)
    return build_user_out(u)


@app.delete("/api/admin/users/{user_id}", status_code=204)
async def admin_delete_user(
    user_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Vous ne pouvez pas supprimer votre propre compte")
    r = await session.execute(select(User).where(User.id == user_id))
    u = r.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404)
    if u.is_admin:
        n = await session.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True)))
        if n is not None and n <= 1:
            raise HTTPException(
                status_code=400,
                detail="Impossible de supprimer le dernier administrateur",
            )
    remove_avatar_file(AVATAR_UPLOAD_DIR, u.avatar_filename)
    await session.delete(u)
    await session.commit()


@app.websocket("/ws/channel/{channel_id}")
async def ws_channel(websocket: WebSocket, channel_id: int):
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401)
        return
    ser = URLSafeTimedSerializer(SECRET_KEY, salt=WS_TOKEN_SALT)
    try:
        data = ser.loads(token, max_age=SESSION_MAX_AGE)
        user_id = data.get("uid")
    except (BadSignature, SignatureExpired):
        await websocket.close(code=4401)
        return
    if not user_id:
        await websocket.close(code=4401)
        return

    async with SessionLocal() as dbs:
        r = await dbs.execute(
            select(Channel).options(selectinload(Channel.chat_group)).where(Channel.id == channel_id)
        )
        ch = r.scalar_one_or_none()
        if ch is None:
            await websocket.close(code=4404)
            return
        ur = await dbs.execute(select(User).where(User.id == int(user_id)))
        u = ur.scalar_one_or_none()
        if not u or not u.is_active:
            await websocket.close(code=4401)
            return
        try:
            await ensure_channel_access(dbs, u, ch)
        except HTTPException:
            await websocket.close(code=4403)
            return

    await hub.connect(channel_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(channel_id, websocket)
