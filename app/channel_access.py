from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.group_service import is_chat_group_member
from app.models import Channel, User

KIND_STANDARD = "standard"
KIND_DIRECT = "direct"


def channel_kind(ch: Channel) -> str:
    return ch.kind or KIND_STANDARD


async def ensure_channel_access(session: AsyncSession, user: User, ch: Channel) -> None:
    if channel_kind(ch) == KIND_DIRECT:
        if ch.dm_user_low_id is None or ch.dm_user_high_id is None:
            raise HTTPException(status_code=500, detail="Canal direct invalide")
        if user.id not in (ch.dm_user_low_id, ch.dm_user_high_id):
            raise HTTPException(status_code=403, detail="Accès à cette conversation refusé")
        return
    if ch.group_id is None:
        return
    if user.is_admin:
        return
    if await is_chat_group_member(session, user.id, ch.group_id):
        return
    raise HTTPException(status_code=403, detail="Accès à ce canal de groupe refusé")
