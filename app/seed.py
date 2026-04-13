from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_utils import hash_password
from app.models import Channel, User


async def ensure_seed_data(session: AsyncSession) -> None:
    r = await session.execute(select(User).limit(1))
    if r.scalar_one_or_none() is None:
        session.add(
            User(
                email="admin@entreprise.local",
                display_name="Administrateur",
                password_hash=hash_password("admin123"),
                is_admin=True,
                is_active=True,
            )
        )
        await session.flush()

    rc = await session.execute(select(Channel).where(Channel.name == "general"))
    if rc.scalar_one_or_none() is None:
        session.add(Channel(name="general", topic="Canal général", is_private=False))

    await session.commit()
