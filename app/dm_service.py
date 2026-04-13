from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channel_access import KIND_DIRECT
from app.models import Channel


async def get_or_create_dm_channel(session: AsyncSession, user_a: int, user_b: int) -> Channel:
    if user_a == user_b:
        raise ValueError("impossible")
    low, high = min(user_a, user_b), max(user_a, user_b)
    r = await session.execute(
        select(Channel).where(
            Channel.kind == KIND_DIRECT,
            Channel.dm_user_low_id == low,
            Channel.dm_user_high_id == high,
        )
    )
    existing = r.scalar_one_or_none()
    if existing:
        return existing
    ch = Channel(
        name=f"dm-{low}-{high}",
        kind=KIND_DIRECT,
        dm_user_low_id=low,
        dm_user_high_id=high,
        is_private=True,
        topic=None,
        created_by_id=None,
    )
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    return ch
