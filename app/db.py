import ssl
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import BASE_DIR, DATABASE_URL, IS_SQLITE_DB, USE_POSTGRES_SSL

(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)

_pg_connect: dict = {}
if not IS_SQLITE_DB and USE_POSTGRES_SSL:
    _pg_connect["ssl"] = ssl.create_default_context()

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args=_pg_connect,
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if not IS_SQLITE_DB:
            return
        r = await conn.execute(text("PRAGMA table_info(users)"))
        cols = [row[1] for row in r.fetchall()]
        if "avatar_filename" not in cols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN avatar_filename VARCHAR(255)"))
        rm = await conn.execute(text("PRAGMA table_info(messages)"))
        mcols = [row[1] for row in rm.fetchall()]
        if "edited_at" not in mcols:
            await conn.execute(text("ALTER TABLE messages ADD COLUMN edited_at DATETIME"))
        rch = await conn.execute(text("PRAGMA table_info(channels)"))
        chcols = [row[1] for row in rch.fetchall()]
        if "created_by_id" not in chcols:
            await conn.execute(text("ALTER TABLE channels ADD COLUMN created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL"))
        for col, ddl in (
            ("kind", "ALTER TABLE channels ADD COLUMN kind VARCHAR(16) DEFAULT 'standard'"),
            ("dm_user_low_id", "ALTER TABLE channels ADD COLUMN dm_user_low_id INTEGER REFERENCES users(id) ON DELETE CASCADE"),
            ("dm_user_high_id", "ALTER TABLE channels ADD COLUMN dm_user_high_id INTEGER REFERENCES users(id) ON DELETE CASCADE"),
            ("last_activity_at", "ALTER TABLE channels ADD COLUMN last_activity_at DATETIME"),
        ):
            rp = await conn.execute(text("PRAGMA table_info(channels)"))
            cur_cols = [row[1] for row in rp.fetchall()]
            if col not in cur_cols:
                await conn.execute(text(ddl))
        for col, ddl in (
            ("last_login_at", "ALTER TABLE users ADD COLUMN last_login_at DATETIME"),
            ("last_logout_at", "ALTER TABLE users ADD COLUMN last_logout_at DATETIME"),
            ("last_seen_at", "ALTER TABLE users ADD COLUMN last_seen_at DATETIME"),
        ):
            rp = await conn.execute(text("PRAGMA table_info(users)"))
            cur_cols = [row[1] for row in rp.fetchall()]
            if col not in cur_cols:
                await conn.execute(text(ddl))
        rch2 = await conn.execute(text("PRAGMA table_info(channels)"))
        chcols2 = [row[1] for row in rch2.fetchall()]
        if "group_id" not in chcols2:
            await conn.execute(
                text(
                    "ALTER TABLE channels ADD COLUMN group_id INTEGER REFERENCES chat_groups(id) ON DELETE CASCADE"
                )
            )
