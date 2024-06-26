from typing import Generator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.database import Base, async_engine, async_session, engine
from app.api.main import app
from tests.factories import _Session


@pytest_asyncio.fixture
async def async_db_session():
    async with async_session() as session:
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield session
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def db() -> Generator:
    Base.metadata.create_all(bind=engine)
    with _Session() as db_session:
        try:
            yield db_session
        finally:
            db_session.close()
            Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db) -> Generator:
    with TestClient(app) as c:
        yield c
