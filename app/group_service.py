from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Channel, ChatGroup, ChatGroupMember, User
from app.slug_utils import is_reserved_dm_slug

_KIND_STANDARD = "standard"


async def is_chat_group_member(session: AsyncSession, user_id: int, group_id: int) -> bool:
    r = await session.execute(
        select(ChatGroupMember).where(
            ChatGroupMember.group_id == group_id,
            ChatGroupMember.user_id == user_id,
        )
    )
    return r.scalar_one_or_none() is not None


def user_can_manage_chat_group(user: User, group: ChatGroup) -> bool:
    return user.is_admin or (group.created_by_id is not None and group.created_by_id == user.id)


async def ensure_chat_group_membership(session: AsyncSession, user_id: int, group_id: int) -> bool:
    return await is_chat_group_member(session, user_id, group_id)


async def list_global_standard_channels(session: AsyncSession) -> list[Channel]:
    r = await session.execute(
        select(Channel)
        .where(
            or_(Channel.kind == _KIND_STANDARD, Channel.kind.is_(None)),
            Channel.group_id.is_(None),
        )
        .options(selectinload(Channel.chat_group))
        .order_by(Channel.name)
    )
    return list(r.scalars().all())


async def list_group_ids_for_user(session: AsyncSession, user_id: int) -> list[int]:
    r = await session.execute(
        select(ChatGroupMember.group_id).where(ChatGroupMember.user_id == user_id)
    )
    return list(r.scalars().all())


async def list_standard_channels_for_user(session: AsyncSession, user: User) -> list[Channel]:
    """Canaux standards visibles : globaux + ceux des groupes dont l’utilisateur est membre (+ tout si admin)."""
    base = select(Channel).where(or_(Channel.kind == _KIND_STANDARD, Channel.kind.is_(None)))
    if user.is_admin:
        r = await session.execute(
            base.options(selectinload(Channel.chat_group)).order_by(Channel.name)
        )
        return list(r.scalars().all())
    gids = await list_group_ids_for_user(session, user.id)
    if not gids:
        r = await session.execute(
            base.where(Channel.group_id.is_(None))
            .options(selectinload(Channel.chat_group))
            .order_by(Channel.name)
        )
        return list(r.scalars().all())
    r = await session.execute(
        base.where(or_(Channel.group_id.is_(None), Channel.group_id.in_(gids)))
        .options(selectinload(Channel.chat_group))
        .order_by(Channel.name)
    )
    return list(r.scalars().all())


async def list_chat_groups_for_user(session: AsyncSession, user: User) -> list[ChatGroup]:
    if user.is_admin:
        r = await session.execute(select(ChatGroup).order_by(ChatGroup.name))
        return list(r.scalars().all())
    r = await session.execute(
        select(ChatGroup)
        .join(ChatGroupMember, ChatGroupMember.group_id == ChatGroup.id)
        .where(ChatGroupMember.user_id == user.id)
        .order_by(ChatGroup.name)
    )
    return list(r.scalars().all())


async def channels_for_group(session: AsyncSession, group_id: int) -> list[Channel]:
    r = await session.execute(
        select(Channel)
        .where(
            Channel.group_id == group_id,
            or_(Channel.kind == _KIND_STANDARD, Channel.kind.is_(None)),
        )
        .options(selectinload(Channel.chat_group))
        .order_by(Channel.name)
    )
    return list(r.scalars().all())


async def ensure_default_channel_for_empty_groups(session: AsyncSession) -> None:
    """Pour les groupes sans aucun canal (anciennes données), crée un canal #accueil."""
    r = await session.execute(select(ChatGroup))
    groups = list(r.scalars().all())
    changed = False
    for g in groups:
        n = await session.scalar(
            select(func.count()).select_from(Channel).where(Channel.group_id == g.id)
        )
        if n and n > 0:
            continue
        acc_suffix = "-accueil"
        acc_base_max = 80 - len(acc_suffix)
        acc_name = (g.slug[:acc_base_max] + acc_suffix)[:80]
        if is_reserved_dm_slug(acc_name):
            acc_name = (g.slug[: max(1, acc_base_max - 2)] + "-acc")[:80]
        exists = await session.execute(select(Channel).where(Channel.name == acc_name))
        if exists.scalar_one_or_none():
            acc_name = (g.slug[: max(1, acc_base_max - 3)] + "-a1")[:80]
        session.add(
            Channel(
                name=acc_name,
                topic="Canal principal du groupe — bienvenue !",
                is_private=False,
                created_by_id=g.created_by_id,
                group_id=g.id,
            )
        )
        changed = True
    if changed:
        await session.commit()


async def unique_chat_group_slug(session: AsyncSession, base: str) -> str:
    slug = base[:80]
    candidate = slug
    n = 0
    while True:
        r = await session.execute(select(ChatGroup).where(ChatGroup.slug == candidate))
        if r.scalar_one_or_none() is None:
            return candidate
        n += 1
        suffix = f"-{n}"
        candidate = (slug[: 80 - len(suffix)] + suffix)[:80]
