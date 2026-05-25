import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import absbot.service as service_module
from absbot.abs_client import AudiobookshelfNotFoundError
from absbot.models import Base


@pytest.fixture(autouse=True)
def registration_lock():
    service_module._REGISTRATION_LOCK = asyncio.Lock()


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


class FakeAudiobookshelfClient:
    def __init__(self):
        self.created = []
        self.create_user_error = None
        self.reset = []
        self.deleted = []
        self.disabled = []
        self.restored = []
        self.users = {}
        self.auth_users = {}
        self.latest_playback = {}
        # abs_user_id -> list[dict] (newest first) for get_listening_sessions_page
        self.sessions_db: dict[str, list[dict]] = {}

    async def create_user(self, username: str, password: str):
        if self.create_user_error is not None:
            raise self.create_user_error
        user_id = f"usr_{len(self.created) + 1}"
        user = {"id": user_id, "username": username, "lastSeen": None, "isActive": True}
        self.created.append((username, password))
        self.users[user_id] = user
        return user

    async def list_users(self):
        return list(self.users.values())

    async def reset_password(self, abs_user_id: str, password: str):
        self.reset.append((abs_user_id, password))
        return self.users.get(abs_user_id, {"id": abs_user_id})

    async def delete_user(self, abs_user_id: str):
        self.deleted.append(abs_user_id)

    async def disable_user(self, abs_user_id: str):
        self.disabled.append(abs_user_id)

    async def restore_user(self, abs_user_id: str):
        self.restored.append(abs_user_id)

    async def get_user(self, abs_user_id: str):
        if abs_user_id not in self.users:
            raise AudiobookshelfNotFoundError("用户不存在")
        return self.users[abs_user_id]

    async def get_latest_listening_session(self, abs_user_id: str):
        return self.latest_playback.get(abs_user_id)

    async def get_listening_sessions_page(
        self, abs_user_id: str, *, page: int = 0, items_per_page: int = 50
    ) -> dict:
        all_sessions = self.sessions_db.get(abs_user_id, [])
        start = page * items_per_page
        return {"sessions": all_sessions[start : start + items_per_page]}

    async def authenticate_user(self, username: str, password: str):
        user = self.auth_users.get((username, password))
        if user is None:
            raise ValueError("账号或密码错误")
        return user


@pytest.fixture
def abs_client():
    return FakeAudiobookshelfClient()
