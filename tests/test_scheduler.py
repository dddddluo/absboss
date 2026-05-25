from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.methods import SendMessage

from absbot.backup import list_local_backups
from absbot.config import Settings
from absbot.scheduler import (
    notify_expiration_result,
    notify_registration_result,
    process_registration_queue_once,
    run_backup_job,
    run_registration_queue_worker,
    safe_send_message,
)
from absbot.service import (
    ExpirationProcessResult,
    ExpirationUserResult,
    RegistrationQueueProcessResult,
    SystemSettings,
)


class FakeBot:
    def __init__(self, *side_effects):
        self.side_effects = list(side_effects)
        self.sent_messages = []

    async def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))
        if self.side_effects:
            effect = self.side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
        return object()


class FakeRegistrationQueueService:
    def __init__(self, result: RegistrationQueueProcessResult):
        self.result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.notified = []

    async def get_next_registration_notification(self):
        return None

    async def process_next_registration(self):
        self.started.set()
        await self.release.wait()
        return self.result

    async def mark_registration_queue_notified(self, queue_id: int):
        self.notified.append(queue_id)

    async def get_public_settings(self):
        return SimpleNamespace(server_lines="line-a")


class FlakyRegistrationQueueService:
    def __init__(self):
        self.attempts = 0
        self.second_attempt = asyncio.Event()

    async def process_next_registration(self):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("transient failure")
        self.second_attempt.set()
        return None

    async def get_next_registration_notification(self):
        return None


class PendingNotificationService:
    def __init__(self, result: RegistrationQueueProcessResult):
        self.result = result
        self.process_calls = 0
        self.notified = []

    async def get_next_registration_notification(self):
        return None if self.notified else self.result

    async def process_next_registration(self):
        self.process_calls += 1
        return None

    async def mark_registration_queue_notified(self, queue_id: int):
        self.notified.append(queue_id)

    async def get_public_settings(self):
        return SimpleNamespace(server_lines="line-a")


class PendingRegistrationWithOldNotificationService:
    def __init__(
        self,
        old_result: RegistrationQueueProcessResult,
        new_result: RegistrationQueueProcessResult,
    ):
        self.old_result = old_result
        self.new_result = new_result
        self.notification_calls = 0
        self.process_calls = 0
        self.notified = []

    async def get_next_registration_notification(self):
        self.notification_calls += 1
        return self.old_result

    async def process_next_registration(self):
        self.process_calls += 1
        return self.new_result

    async def mark_registration_queue_notified(self, queue_id: int):
        self.notified.append(queue_id)

    async def get_public_settings(self):
        return SimpleNamespace(server_lines="line-a")


def _system(main_group_chat_id: int | None = -100123) -> SystemSettings:
    return SystemSettings(
        default_register_days=30,
        panel_photo_path=None,
        rebind_review_chat_id=None,
        main_group_chat_id=main_group_chat_id,
        main_group_link=None,
        disabled_delete_after_days=0,
    )


def _empty_result() -> ExpirationProcessResult:
    return ExpirationProcessResult(
        active_renewed=[],
        points_renewed=[],
        disabled=[],
        deleted=[],
    )


def _user(
    telegram_id: int,
    username: str | None,
    *,
    disabled_at: datetime | None = None,
) -> ExpirationUserResult:
    return ExpirationUserResult(
        telegram_id=telegram_id,
        abs_user_id=f"abs-{telegram_id}",
        abs_username=username,
        expires_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        disabled_at=disabled_at,
    )


def _retry_after(retry_after: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SendMessage(chat_id=123, text="test"),
        message="Too Many Requests",
        retry_after=retry_after,
    )


def _api_error() -> TelegramAPIError:
    return TelegramAPIError(
        method=SendMessage(chat_id=123, text="test"),
        message="Bad Request",
    )


async def test_notify_expiration_result_sends_group_summary_and_disabled_user_dm():
    bot = FakeBot()
    disabled = _user(
        42,
        "disabled <user>",
        disabled_at=datetime(2026, 5, 21, 4, 10, tzinfo=timezone.utc),
    )
    result = ExpirationProcessResult(
        active_renewed=[_user(1, "active")],
        points_renewed=[_user(2, "points")],
        disabled=[disabled],
        deleted=[_user(3, "deleted")],
    )

    await notify_expiration_result(bot, _system(), result)

    assert len(bot.sent_messages) == 4
    assert bot.sent_messages[0][0] == -100123
    assert "活跃续期：1" in bot.sent_messages[0][1]
    assert "积分续期：1" in bot.sent_messages[0][1]
    assert "已禁用：1" in bot.sent_messages[0][1]
    assert "已删除：1" in bot.sent_messages[0][1]
    assert bot.sent_messages[3][0] == 42
    assert "disabled &lt;user&gt;" in bot.sent_messages[3][1]


async def test_notify_expiration_result_empty_result_sends_nothing():
    bot = FakeBot()

    await notify_expiration_result(bot, _system(), _empty_result())

    assert bot.sent_messages == []


async def test_notify_registration_result_sends_success_message():
    bot = FakeBot()
    result = RegistrationQueueProcessResult(
        queue_id=1,
        telegram_id=96001,
        success=True,
        username="alice",
        initial_password="secret",
        expires_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )

    sent = await notify_registration_result(bot, "line-a", result)

    assert sent is True
    assert len(bot.sent_messages) == 1
    chat_id, text = bot.sent_messages[0]
    assert chat_id == 96001
    assert "账号创建成功" in text
    assert "alice" in text
    assert "secret" in text
    assert "line-a" in text


async def test_notify_registration_result_sends_failure_message():
    bot = FakeBot()
    result = RegistrationQueueProcessResult(
        queue_id=2,
        telegram_id=96002,
        success=False,
        error_message="当前没有可用注册资格",
    )

    sent = await notify_registration_result(bot, "line-a", result)

    assert sent is True
    assert len(bot.sent_messages) == 1
    chat_id, text = bot.sent_messages[0]
    assert chat_id == 96002
    assert "注册失败" in text
    assert "当前没有可用注册资格" in text


async def test_notify_registration_result_escapes_server_lines():
    bot = FakeBot()
    result = RegistrationQueueProcessResult(
        queue_id=3,
        telegram_id=96003,
        success=True,
        username="alice",
        initial_password="secret",
        expires_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )

    sent = await notify_registration_result(bot, "line <a&b>", result)

    assert sent is True
    assert len(bot.sent_messages) == 1
    assert "line &lt;a&amp;b&gt;" in bot.sent_messages[0][1]


async def test_notify_registration_result_returns_false_on_delivery_failure():
    bot = FakeBot(_api_error())
    result = RegistrationQueueProcessResult(
        queue_id=4,
        telegram_id=96004,
        success=False,
        error_message="当前没有可用注册资格",
    )

    sent = await notify_registration_result(bot, "line-a", result)

    assert sent is False


async def test_registration_queue_worker_finishes_current_cycle_when_cancelled():
    bot = FakeBot()
    result = RegistrationQueueProcessResult(
        queue_id=5,
        telegram_id=96005,
        success=True,
        username="alice",
        initial_password="secret",
        expires_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    service = FakeRegistrationQueueService(result)
    worker = asyncio.create_task(
        run_registration_queue_worker(service, bot, empty_sleep_seconds=999)
    )

    await service.started.wait()
    worker.cancel()
    await asyncio.sleep(0)
    assert worker.done() is False

    service.release.set()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert len(bot.sent_messages) == 1
    assert bot.sent_messages[0][0] == 96005
    assert service.notified == [5]


async def test_process_registration_queue_once_retries_unnotified_result_after_delivery_failure():
    result = RegistrationQueueProcessResult(
        queue_id=6,
        telegram_id=96006,
        success=True,
        username="alice",
        initial_password="secret",
        expires_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    service = PendingNotificationService(result)
    failed_bot = FakeBot(_api_error())

    processed = await process_registration_queue_once(service, failed_bot)
    retried = await process_registration_queue_once(service, FakeBot())

    assert processed is False
    assert retried is True
    assert service.process_calls == 2
    assert service.notified == [6]


async def test_process_registration_queue_once_processes_pending_before_old_notification():
    old_result = RegistrationQueueProcessResult(
        queue_id=7,
        telegram_id=96007,
        success=False,
        error_message="当前没有可用注册资格",
    )
    new_result = RegistrationQueueProcessResult(
        queue_id=8,
        telegram_id=96008,
        success=True,
        username="new-user",
        initial_password="secret",
        expires_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    service = PendingRegistrationWithOldNotificationService(old_result, new_result)
    bot = FakeBot()

    processed = await process_registration_queue_once(service, bot)

    assert processed is True
    assert service.process_calls == 1
    assert service.notification_calls == 0
    assert service.notified == [8]
    assert bot.sent_messages[0][0] == 96008
    assert "new-user" in bot.sent_messages[0][1]


async def test_registration_queue_worker_continues_after_processing_error():
    service = FlakyRegistrationQueueService()
    worker = asyncio.create_task(
        run_registration_queue_worker(service, FakeBot(), empty_sleep_seconds=0.01)
    )

    await asyncio.wait_for(service.second_attempt.wait(), timeout=1)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert service.attempts >= 2


async def test_safe_send_message_waits_retry_after_and_retries(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("absbot.scheduler.asyncio.sleep", fake_sleep)
    bot = FakeBot(_retry_after(7))

    sent = await safe_send_message(bot, 456, "hello")

    assert sent is True
    assert sleeps == [7]
    assert bot.sent_messages == [(456, "hello"), (456, "hello")]


async def test_safe_send_message_bounds_retry_after_attempts(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("absbot.scheduler.asyncio.sleep", fake_sleep)
    bot = FakeBot(_retry_after(2), _retry_after(2), _retry_after(2))

    sent = await safe_send_message(bot, 456, "hello", max_attempts=3)

    assert sent is False
    assert sleeps == [2, 2]
    assert bot.sent_messages == [(456, "hello"), (456, "hello"), (456, "hello")]


async def test_safe_send_message_rejects_retry_after_above_cap_without_sleep(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("absbot.scheduler.asyncio.sleep", fake_sleep)
    bot = FakeBot(_retry_after(3600))

    sent = await safe_send_message(bot, 456, "hello", max_retry_after=60)

    assert sent is False
    assert sleeps == []
    assert bot.sent_messages == [(456, "hello")]


async def test_safe_send_message_returns_false_on_telegram_api_error():
    bot = FakeBot(_api_error())

    sent = await safe_send_message(bot, 456, "hello")

    assert sent is False
    assert bot.sent_messages == [(456, "hello")]


# ── Backup job tests ──────────────────────────────────────────────────────────


class FakeBotWithDocument:
    def __init__(self):
        self.sent_messages: list[tuple] = []
        self.sent_documents: list[tuple] = []

    async def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))

    async def send_document(self, chat_id, document, caption=None):
        self.sent_documents.append((chat_id, document, caption))


def _backup_settings(tmp_path, owner_tg_id=777) -> Settings:
    return Settings(
        bot_token="token",
        admin_tg_ids={777},
        mysql_dsn="sqlite+aiosqlite:///:memory:",
        abs_base_url="https://abs.example.com",
        abs_api_token="tok",
        owner_tg_id=owner_tg_id,
        backup_dir=str(tmp_path / "backups"),
        backup_keep_count=3,
    )


async def test_run_backup_job_creates_file_and_sends_to_owner(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from absbot.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    bot = FakeBotWithDocument()
    settings = _backup_settings(tmp_path)

    await run_backup_job(engine, bot, settings)

    backups = list_local_backups(settings.backup_dir)
    assert len(backups) == 1
    assert backups[0].name.startswith("backup_")
    assert len(bot.sent_documents) == 1
    assert bot.sent_documents[0][0] == 777


async def test_run_backup_job_skips_send_when_no_owner(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from absbot.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    bot = FakeBotWithDocument()
    settings = _backup_settings(tmp_path, owner_tg_id=None)

    await run_backup_job(engine, bot, settings)

    backups = list_local_backups(settings.backup_dir)
    assert len(backups) == 1
    assert bot.sent_documents == []


async def test_run_backup_job_cleans_up_old_backups(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for name in [
        "backup_20260519_050000.sql",
        "backup_20260520_050000.sql",
        "backup_20260521_050000.sql",
    ]:
        (backup_dir / name).write_text("-- old")

    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from absbot.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    bot = FakeBotWithDocument()
    settings = _backup_settings(tmp_path)  # keep_count=3

    await run_backup_job(engine, bot, settings)

    remaining = list_local_backups(settings.backup_dir)
    assert len(remaining) == 3  # 3 old + 1 new → keep newest 3 → 1 old removed
