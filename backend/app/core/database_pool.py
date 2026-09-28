import logging

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings

logger = logging.getLogger(__name__)


class DatabasePool:
    def __init__(self):
        self.engine = None
        self.session_factory = None

    async def initialize(self):
        """Reuse one async pool connected to the configured PostgreSQL database."""
        if self.engine is not None:
            return

        database_url = make_url(settings.database_url).set(drivername="postgresql+asyncpg")
        self.engine = create_async_engine(
            database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout,
            pool_pre_ping=True,
            pool_recycle=settings.database_pool_recycle,
            connect_args={"timeout": 5},
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine, class_=AsyncSession, expire_on_commit=False
        )
        logger.info("Database connection pool initialized")

    async def close(self):
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            self.session_factory = None

    def get_session(self) -> AsyncSession:
        """Return the async context manager itself, not a coroutine wrapping it."""
        if self.session_factory is None:
            raise RuntimeError("Database pool not initialized")
        return self.session_factory()


db_pool = DatabasePool()


async def get_db_session():
    await db_pool.initialize()
    async with db_pool.get_session() as session:
        yield session
