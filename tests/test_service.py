from datetime import date, datetime, timedelta, timezone

import asyncio
import pytest
from sqlalchemy import select, func

from absbot.abs_client import AudiobookshelfError
from absbot.models import (
    RebindRequestStatus,
    RedeemCodeType,
    RegistrationQueue,
    RegistrationQueueStatus,
    TgUser,
)
from absbot.service import AccountCreationResult, MembershipService, SyncResult
from absbot.timeutils import ensure_utc

UTC = timezone.utc


async def test_registration_queue_model_and_user_password_column(session_factory):
    async with session_factory() as session:
        async with session.begin():
            user = TgUser(telegram_id=91001, abs_password="secret")
            queue_item = RegistrationQueue(
                telegram_id=91001,
                abs_username="queued-user",
                status=RegistrationQueueStatus.PENDING,
                position=1,
            )
            session.add_all([user, queue_item])

        saved_user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 91001))
        saved_item = await session.scalar(
            select(RegistrationQueue).where(RegistrationQueue.telegram_id == 91001)
        )

    assert saved_user is not None
    assert saved_user.abs_password == "secret"
    assert saved_item is not None
    assert saved_item.abs_username == "queued-user"
    assert saved_item.status == RegistrationQueueStatus.PENDING
    assert saved_item.notification_delivered is False


async def test_enqueue_registration_creates_pending_queue_item(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    position = await service.enqueue_registration(telegram_id=92001, abs_username="alice")
    current_position = await service.get_queue_position(92001)

    async with session_factory() as session:
        item = await session.scalar(
            select(RegistrationQueue).where(RegistrationQueue.telegram_id == 92001)
        )

    assert position == 1
    assert current_position == 1
    assert item is not None
    assert item.abs_username == "alice"
    assert item.status == RegistrationQueueStatus.PENDING


async def test_enqueue_registration_returns_existing_position(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    first = await service.enqueue_registration(telegram_id=92002, abs_username="alice")
    second = await service.enqueue_registration(telegram_id=92002, abs_username="bob")

    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(RegistrationQueue).where(RegistrationQueue.telegram_id == 92002)
                )
            ).all()
        )

    assert first == 1
    assert second == 1
    assert len(rows) == 1
    assert rows[0].abs_username == "alice"


async def test_enqueue_registration_serializes_duplicate_across_service_instances(
    session_factory, abs_client
):
    service_a = MembershipService(session_factory, abs_client)
    service_b = MembershipService(session_factory, abs_client)

    first, second = await asyncio.gather(
        service_a.enqueue_registration(telegram_id=92004, abs_username="alice"),
        service_b.enqueue_registration(telegram_id=92004, abs_username="bob"),
    )

    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(RegistrationQueue).where(RegistrationQueue.telegram_id == 92004)
                )
            ).all()
        )

    assert first == 1
    assert second == 1
    assert len(rows) == 1
    assert rows[0].abs_username in {"alice", "bob"}


async def test_enqueue_registration_rejects_existing_account(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)
    await service.create_account_from_registration(telegram_id=92003, username="alice")

    with pytest.raises(ValueError, match="已经创建过账号"):
        await service.enqueue_registration(telegram_id=92003, abs_username="again")


async def test_open_registration_consumes_last_slot_and_closes(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)

    result = await service.create_account_from_registration(telegram_id=1001, username="alice")

    settings = await service.get_public_settings()
    profile = await service.get_profile(1001)
    assert result.username == "alice"
    assert result.initial_password
    assert settings.registration_open is False
    assert settings.registration_slots == 0
    assert profile.abs_user_id == "usr_1"


async def test_create_account_persists_generated_password(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)

    result = await service.create_account_from_registration(telegram_id=93001, username="alice")
    profile = await service.get_profile(93001)

    assert result.initial_password
    assert profile.abs_user_id == "usr_1"
    async with session_factory() as session:
        user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 93001))
    assert user is not None
    assert user.abs_password == result.initial_password


async def test_process_next_registration_success_marks_done(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)
    await service.enqueue_registration(telegram_id=94001, abs_username="alice")

    result = await service.process_next_registration()

    assert result is not None
    assert result.success is True
    assert result.telegram_id == 94001
    assert result.username == "alice"
    assert result.initial_password
    async with session_factory() as session:
        item = await session.scalar(
            select(RegistrationQueue).where(RegistrationQueue.telegram_id == 94001)
        )
        user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 94001))
    assert result.queue_id == item.id
    assert item.status == RegistrationQueueStatus.DONE
    assert item.notification_delivered is False
    assert item.result_password == result.initial_password
    assert ensure_utc(item.result_expires_at) == result.expires_at
    assert item.processed_at is not None
    assert user.abs_password == result.initial_password


async def test_process_next_registration_waits_before_creating_account_when_delay_configured(
    session_factory, abs_client
):
    service = MembershipService(
        session_factory,
        abs_client,
        registration_queue_delay_seconds=0.02,
    )
    await service.set_registration(opened=True, slots=1)
    await service.enqueue_registration(telegram_id=94007, abs_username="alice")
    create_started = asyncio.Event()

    async def fake_create_account(telegram_id: int, username: str, **_kwargs):
        create_started.set()
        return AccountCreationResult(
            abs_user_id="usr_1",
            username=username,
            initial_password="secret",
            expires_at=None,
        )

    service.create_account_from_registration = fake_create_account

    task = asyncio.create_task(service.process_next_registration())
    await asyncio.sleep(0)

    assert create_started.is_set() is False

    result = await task

    assert create_started.is_set() is True
    assert result is not None
    assert result.success is True


async def test_process_next_registration_failure_marks_failed(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.enqueue_registration(telegram_id=94002, abs_username="alice")

    result = await service.process_next_registration()

    assert result is not None
    assert result.success is False
    assert result.error_message == "当前没有可用注册资格"
    async with session_factory() as session:
        item = await session.scalar(
            select(RegistrationQueue).where(RegistrationQueue.telegram_id == 94002)
        )
    assert result.queue_id == item.id
    assert item.status == RegistrationQueueStatus.FAILED
    assert item.notification_delivered is False
    assert item.error_message == "当前没有可用注册资格"
    assert item.processed_at is not None


async def test_get_next_registration_notification_reconstructs_oldest_unnotified_result(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    expires_at = datetime(2026, 5, 23, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                RegistrationQueue(
                    telegram_id=94005,
                    abs_username="alice",
                    status=RegistrationQueueStatus.DONE,
                    result_password="secret",
                    result_expires_at=expires_at,
                    processed_at=datetime(2026, 5, 22, tzinfo=UTC),
                    notification_delivered=False,
                )
            )

    result = await service.get_next_registration_notification()

    assert result is not None
    assert result.telegram_id == 94005
    assert result.success is True
    assert result.username == "alice"
    assert result.initial_password == "secret"
    assert result.expires_at == expires_at


async def test_mark_registration_queue_notified_hides_result_from_notification_retry(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    async with session_factory() as session:
        async with session.begin():
            item = RegistrationQueue(
                telegram_id=94006,
                abs_username="alice",
                status=RegistrationQueueStatus.FAILED,
                error_message="当前没有可用注册资格",
                processed_at=datetime(2026, 5, 22, tzinfo=UTC),
            )
            session.add(item)
            await session.flush()
            queue_id = item.id

    pending = await service.get_next_registration_notification()
    await service.mark_registration_queue_notified(queue_id)
    retry = await service.get_next_registration_notification()

    assert pending is not None
    assert pending.queue_id == queue_id
    assert pending.success is False
    assert pending.error_message == "当前没有可用注册资格"
    assert retry is None


async def test_process_next_registration_serializes_concurrent_calls(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)
    await service.enqueue_registration(telegram_id=94003, abs_username="alice")
    await service.enqueue_registration(telegram_id=94004, abs_username="bob")
    create_started = asyncio.Event()
    release_create = asyncio.Event()
    create_calls = 0

    async def blocked_create_account(telegram_id: int, username: str, **_kwargs):
        nonlocal create_calls
        create_calls += 1
        if create_calls > 1:
            raise ValueError("当前没有可用注册资格")
        create_started.set()
        await release_create.wait()
        abs_client.created.append((username, "password"))
        return AccountCreationResult(
            abs_user_id="usr_queued",
            username=username,
            initial_password="password",
            expires_at=None,
        )

    service.create_account_from_registration = blocked_create_account

    first_task = asyncio.create_task(service.process_next_registration())
    await create_started.wait()
    second_task = asyncio.create_task(service.process_next_registration())
    second_done, _ = await asyncio.wait({second_task}, timeout=0.05)

    release_create.set()
    results = await asyncio.gather(first_task, second_task)

    assert not second_done
    successes = [result for result in results if result and result.success]
    failures = [result for result in results if result and not result.success]
    async with session_factory() as session:
        final_statuses = list(
            (
                await session.scalars(
                    select(RegistrationQueue.status).where(
                        RegistrationQueue.telegram_id.in_([94003, 94004])
                    )
                )
            ).all()
        )
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].error_message == "当前没有可用注册资格"
    assert len(abs_client.created) == 1
    assert final_statuses.count(RegistrationQueueStatus.DONE) == 1
    assert final_statuses.count(RegistrationQueueStatus.FAILED) == 1


async def test_process_next_registration_returns_none_when_empty(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    assert await service.process_next_registration() is None


async def test_reset_stuck_queue_items_returns_processing_to_pending(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                RegistrationQueue(
                    telegram_id=95001,
                    abs_username="alice",
                    status=RegistrationQueueStatus.PROCESSING,
                    position=1,
                )
            )

    count = await service.reset_stuck_queue_items()

    async with session_factory() as session:
        item = await session.scalar(
            select(RegistrationQueue).where(RegistrationQueue.telegram_id == 95001)
        )
    assert count == 1
    assert item.status == RegistrationQueueStatus.PENDING


async def test_process_next_registration_recovers_previously_created_account_after_reset(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    expires_at = datetime(2026, 5, 24, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                TgUser(
                    telegram_id=95002,
                    abs_user_id="usr_existing",
                    abs_username="alice",
                    abs_password="stored-secret",
                    expires_at=expires_at,
                )
            )
            session.add(
                RegistrationQueue(
                    telegram_id=95002,
                    abs_username="alice",
                    status=RegistrationQueueStatus.PROCESSING,
                    position=1,
                )
            )

    assert await service.reset_stuck_queue_items() == 1
    result = await service.process_next_registration()

    async with session_factory() as session:
        item = await session.scalar(
            select(RegistrationQueue).where(RegistrationQueue.telegram_id == 95002)
        )
    assert result is not None
    assert result.success is True
    assert result.username == "alice"
    assert result.initial_password == "stored-secret"
    assert result.expires_at == expires_at
    assert item.status == RegistrationQueueStatus.DONE
    assert item.result_password == "stored-secret"
    assert item.result_expires_at is not None
    assert item.notification_delivered is False
    assert item.processed_at is not None
    assert abs_client.created == []


async def test_process_next_registration_recovers_reserved_slot_after_reset(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    expires_at = datetime(2026, 5, 24, tzinfo=UTC)
    await service.set_registration(opened=False, slots=0)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                TgUser(
                    telegram_id=95003,
                    expires_at=expires_at,
                )
            )
            session.add(
                RegistrationQueue(
                    telegram_id=95003,
                    abs_username="alice",
                    status=RegistrationQueueStatus.PROCESSING,
                    position=1,
                    result_password="reserved-secret",
                )
            )

    assert await service.reset_stuck_queue_items() == 1
    result = await service.process_next_registration()

    async with session_factory() as session:
        user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 95003))
        item = await session.scalar(
            select(RegistrationQueue).where(RegistrationQueue.telegram_id == 95003)
        )
    assert result is not None
    assert result.success is True
    assert result.initial_password == "reserved-secret"
    assert result.expires_at == expires_at
    assert user.abs_user_id == "usr_1"
    assert user.abs_password == "reserved-secret"
    assert item.status == RegistrationQueueStatus.DONE
    assert abs_client.created == [("alice", "reserved-secret")]


async def test_process_next_registration_relinks_reserved_abs_user_created_before_crash(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    expires_at = datetime(2026, 5, 24, tzinfo=UTC)
    abs_client.users["usr_existing"] = {
        "id": "usr_existing",
        "username": "alice",
        "lastSeen": None,
        "isActive": True,
    }
    await service.set_registration(opened=False, slots=0)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                TgUser(
                    telegram_id=95004,
                    expires_at=expires_at,
                )
            )
            session.add(
                RegistrationQueue(
                    telegram_id=95004,
                    abs_username="alice",
                    status=RegistrationQueueStatus.PROCESSING,
                    position=1,
                    result_password="reserved-secret",
                )
            )

    assert await service.reset_stuck_queue_items() == 1
    result = await service.process_next_registration()

    async with session_factory() as session:
        user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 95004))
        item = await session.scalar(
            select(RegistrationQueue).where(RegistrationQueue.telegram_id == 95004)
        )
    assert result is not None
    assert result.success is True
    assert result.initial_password == "reserved-secret"
    assert result.expires_at == expires_at
    assert user.abs_user_id == "usr_existing"
    assert user.abs_password == "reserved-secret"
    assert item.status == RegistrationQueueStatus.DONE
    assert abs_client.created == []


async def test_create_account_refunds_slot_when_abs_create_fails(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)
    abs_client.create_user_error = AudiobookshelfError("ABS busy")

    with pytest.raises(AudiobookshelfError, match="ABS busy"):
        await service.create_account_from_registration(telegram_id=93002, username="alice")

    settings = await service.get_public_settings()
    profile = await service.get_profile(93002)
    assert settings.registration_open is True
    assert settings.registration_slots == 1
    assert profile.abs_user_id is None
    assert profile.expires_at is None


async def test_create_account_restores_credit_and_expiration_when_abs_create_fails(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    async with session_factory() as session:
        async with session.begin():
            session.add(TgUser(telegram_id=93003, registration_credits=1))
    abs_client.create_user_error = AudiobookshelfError("ABS busy")

    with pytest.raises(AudiobookshelfError, match="ABS busy"):
        await service.create_account_from_registration(telegram_id=93003, username="alice")

    async with session_factory() as session:
        user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 93003))
    assert user is not None
    assert user.registration_credits == 1
    assert user.expires_at is None
    assert user.abs_user_id is None


async def test_create_account_compensates_and_deletes_abs_user_when_db_finalization_fails(
    session_factory, abs_client, monkeypatch
):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)

    class FinalizationError(Exception):
        pass

    def fail_account_creation_result(*args, **kwargs):
        raise FinalizationError("finalization failed")

    monkeypatch.setattr("absbot.service.AccountCreationResult", fail_account_creation_result)

    with pytest.raises(FinalizationError, match="finalization failed"):
        await service.create_account_from_registration(telegram_id=93005, username="alice")

    settings = await service.get_public_settings()
    profile = await service.get_profile(93005)
    assert settings.registration_open is True
    assert settings.registration_slots == 1
    assert profile.abs_user_id is None
    assert profile.expires_at is None
    assert abs_client.deleted == ["usr_1"]


async def test_create_account_serializes_duplicate_creation_for_same_user(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=2)
    original_create_user = abs_client.create_user

    async def delayed_create_user(username: str, password: str):
        await asyncio.sleep(0)
        return await original_create_user(username, password)

    abs_client.create_user = delayed_create_user

    results = await asyncio.gather(
        service.create_account_from_registration(telegram_id=93004, username="alice"),
        service.create_account_from_registration(telegram_id=93004, username="alice2"),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, Exception)]
    settings = await service.get_public_settings()
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert len(abs_client.created) == 1
    assert settings.registration_slots == 1


async def test_registration_announcement_message_metadata_round_trips(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    await service.set_registration_announcement_message(chat_id=-100123, message_id=42)
    assert await service.get_registration_announcement_message() == (-100123, 42)

    await service.set_registration_announcement_message(chat_id=None, message_id=None)
    assert await service.get_registration_announcement_message() == (None, None)


async def test_bind_existing_account_sets_default_expiration(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
    abs_client.auth_users[("alice", "secret")] = {"id": "usr_existing", "username": "alice"}

    result = await service.bind_existing_account(1002, "alice", "secret", now=now)
    profile = await service.get_profile(1002)

    assert result.abs_user_id == "usr_existing"
    assert result.username == "alice"
    assert profile.abs_user_id == "usr_existing"
    assert profile.abs_password == "secret"
    # Uses _extend_from with default_register_days (30), base = max(None, now) = now
    assert profile.expires_at == now + timedelta(days=30)
    assert abs_client.created == []


async def test_bind_existing_account_uses_db_default_register_days(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
    abs_client.auth_users[("alice", "secret")] = {"id": "usr_existing", "username": "alice"}

    await service.set_system_settings(default_register_days=7)
    result = await service.bind_existing_account(10022, "alice", "secret", now=now)
    profile = await service.get_profile(10022)

    assert result.abs_user_id == "usr_existing"
    assert profile.expires_at == now + timedelta(days=7)


async def test_system_settings_store_main_group(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    await service.set_system_settings(
        main_group_chat_id=-1001234567890,
        main_group_link="https://t.me/+abcdef",
    )

    settings = await service.get_system_settings()
    assert settings.main_group_chat_id == -1001234567890
    assert settings.main_group_link == "https://t.me/+abcdef"


async def test_system_settings_store_disabled_delete_after_days(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    default_settings = await service.get_system_settings()
    assert default_settings.disabled_delete_after_days == 0

    await service.set_system_settings(disabled_delete_after_days=7)

    settings = await service.get_system_settings()
    assert settings.disabled_delete_after_days == 7


async def test_system_settings_reject_negative_disabled_delete_after_days(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)

    with pytest.raises(ValueError, match="自动删除天数不能小于 0"):
        await service.set_system_settings(disabled_delete_after_days=-1)


async def test_bind_existing_account_rejects_already_bound_account(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)
    created = await service.create_account_from_registration(1003, "alice")
    abs_client.auth_users[("alice", "secret")] = {"id": created.abs_user_id, "username": "alice"}

    with pytest.raises(ValueError, match="申请换绑"):
        await service.bind_existing_account(1004, "alice", "secret")


async def test_create_rebind_request_returns_created_at(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 22, 10, 30, tzinfo=UTC)
    await service.set_registration(opened=True, slots=1)
    created = await service.create_account_from_registration(1003, "alice")
    abs_client.auth_users[("alice", "secret")] = {"id": created.abs_user_id, "username": "alice"}

    request = await service.create_rebind_request(1004, "alice", "secret", now=now)

    assert request.created_at == now


async def test_approve_rebind_request_replaces_requester_points_with_current_points(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)
    created = await service.create_account_from_registration(1003, "alice")
    await service.admin_adjust_points(1003, delta=25)
    await service.admin_adjust_points(1004, delta=7)
    abs_client.auth_users[("alice", "secret")] = {"id": created.abs_user_id, "username": "alice"}

    request = await service.create_rebind_request(1004, "alice", "secret")
    await service.approve_rebind_request(request.id, reviewer_telegram_id=9001)

    requester = await service.get_profile(1004)
    current = await service.get_profile(1003)
    assert requester.points == 25
    assert current.points == 0


async def test_checkin_respects_admin_toggle_and_only_awards_once_per_day(
    session_factory, abs_client, monkeypatch
):
    service = MembershipService(session_factory, abs_client)
    await service.set_checkin(enabled=True, min_points=3, max_points=8)
    default_settings = await service.get_public_settings()
    assert default_settings.checkin_min_points == 3
    assert default_settings.checkin_max_points == 8

    await service.set_checkin(enabled=False, min_points=5, max_points=9)

    with pytest.raises(ValueError, match="签到未开放"):
        await service.checkin(telegram_id=2001, today=date(2026, 5, 18))

    monkeypatch.setattr("absbot.service.random.randint", lambda start, end: 7)
    await service.set_checkin(enabled=True, min_points=5, max_points=9)
    first = await service.checkin(telegram_id=2001, today=date(2026, 5, 18))
    second = await service.checkin(telegram_id=2001, today=date(2026, 5, 18))

    assert first.points == 7
    assert first.awarded == 7
    assert second.points == 7
    assert second.already_checked_in is True


async def test_checkin_uses_legacy_fixed_points_when_only_old_setting_exists(
    session_factory, abs_client, monkeypatch
):
    service = MembershipService(session_factory, abs_client)
    async with session_factory() as session:
        async with session.begin():
            await service._set_setting(session, "checkin_points", "5")

    settings = await service.get_public_settings()
    monkeypatch.setattr("absbot.service.random.randint", lambda start, end: start)
    result = await service.checkin(telegram_id=2002, today=date(2026, 5, 18))

    assert settings.checkin_min_points == 5
    assert settings.checkin_max_points == 5
    assert result.awarded == 5


async def test_redeem_registration_renewal_and_whitelist_codes(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.create_redeem_code(
        code="REG30",
        code_type=RedeemCodeType.REGISTRATION,
        days=30,
        max_uses=1,
    )
    await service.create_redeem_code(
        code="REN30",
        code_type=RedeemCodeType.RENEWAL,
        days=30,
        max_uses=1,
    )
    await service.create_redeem_code(
        code="WHITE",
        code_type=RedeemCodeType.WHITELIST,
        days=None,
        max_uses=1,
    )

    reg = await service.redeem_code(telegram_id=3001, code="reg30")
    # Registration code should store renewal_days, not set expires_at
    reg_profile = await service.get_profile(3001)
    assert reg.registration_credits == 1
    assert reg_profile.renewal_days == 30
    assert reg_profile.expires_at is None

    await service.create_account_from_registration(telegram_id=3001, username="carol")
    renewal = await service.redeem_code(telegram_id=3001, code="REN30")
    white = await service.redeem_code(telegram_id=3001, code="WHITE")
    profile = await service.get_profile(3001)

    assert renewal.expires_at is not None
    assert white.is_whitelisted is True
    assert profile.is_whitelisted is True


async def test_set_whitelist_restores_disabled_account_and_clears_disabled_at(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_active_retention(enabled=False, window_days=30, extension_days=30)
    await service.set_points_renewal(enabled=False, cost_points=100, extension_days=30)
    await service.grant_registration(
        telegram_id=3201, credits=1, days=1, now=now - timedelta(days=2)
    )
    created = await service.create_account_from_registration(
        telegram_id=3201, username="restored", now=now - timedelta(days=31)
    )

    await service.process_points_renewals(now=now)
    await service.process_expiration_enforcement(now=now)
    disabled_profile = await service.get_profile(3201)

    assert disabled_profile.is_disabled is True
    assert disabled_profile.disabled_at == now

    whitelisted = await service.set_whitelist(3201, True)
    profile = await service.get_profile(3201)

    assert whitelisted.is_whitelisted is True
    assert whitelisted.is_disabled is False
    assert whitelisted.disabled_at is None
    assert profile.is_disabled is False
    assert profile.disabled_at is None
    assert abs_client.restored == [created.abs_user_id]


async def test_grant_registration_days_restores_disabled_account_and_clears_disabled_at(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_active_retention(enabled=False, window_days=30, extension_days=30)
    await service.set_points_renewal(enabled=False, cost_points=100, extension_days=30)
    await service.grant_registration(
        telegram_id=3202, credits=1, days=1, now=now - timedelta(days=2)
    )
    created = await service.create_account_from_registration(
        telegram_id=3202, username="renewed", now=now - timedelta(days=31)
    )

    await service.process_points_renewals(now=now)
    await service.process_expiration_enforcement(now=now)
    disabled_profile = await service.get_profile(3202)

    assert disabled_profile.is_disabled is True
    assert disabled_profile.disabled_at == now

    renewed = await service.grant_registration(telegram_id=3202, credits=0, days=30, now=now)
    profile = await service.get_profile(3202)

    assert renewed.is_disabled is False
    assert renewed.disabled_at is None
    assert renewed.expires_at == now + timedelta(days=30)
    assert profile.is_disabled is False
    assert profile.disabled_at is None
    assert abs_client.restored == [created.abs_user_id]


async def test_grant_registration_caps_registration_credit_at_one(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    first = await service.grant_registration(telegram_id=3203, credits=1)
    second = await service.grant_registration(telegram_id=3203, credits=1)
    profile = await service.get_profile(3203)

    assert first.registration_credits == 1
    assert second.registration_credits == 1
    assert profile.registration_credits == 1


async def test_admin_adjust_expiry_extends_existing_expiration(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.grant_registration(telegram_id=3301, credits=1, days=30, now=now)
    await service.create_account_from_registration(
        telegram_id=3301, username="expiry", now=now - timedelta(days=20)
    )

    updated = await service.admin_adjust_expiry(3301, delta=5, now=now)
    profile = await service.get_profile(3301)

    assert updated.expires_at == now + timedelta(days=15)
    assert profile.expires_at == now + timedelta(days=15)


async def test_admin_adjust_expiry_uses_now_when_expiration_is_missing(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_registration(opened=True, slots=1)
    await service.create_account_from_registration(telegram_id=3302, username="white", now=now)
    await service.set_whitelist(3302, True)
    async with session_factory() as session:
        async with session.begin():
            user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 3302))
            user.expires_at = None

    updated = await service.admin_adjust_expiry(3302, delta=30, now=now)

    assert updated.expires_at == now + timedelta(days=30)


async def test_admin_adjust_expiry_restores_disabled_future_account(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_active_retention(enabled=False, window_days=30, extension_days=30)
    await service.set_points_renewal(enabled=False, cost_points=100, extension_days=30)
    await service.grant_registration(
        telegram_id=3303, credits=1, days=30, now=now - timedelta(days=2)
    )
    created = await service.create_account_from_registration(
        telegram_id=3303, username="disabled", now=now - timedelta(days=31)
    )
    await service.process_points_renewals(now=now)
    await service.process_expiration_enforcement(now=now)

    updated = await service.admin_adjust_expiry(3303, delta=30, now=now)

    assert updated.expires_at == now + timedelta(days=29)
    assert updated.is_disabled is False
    assert updated.disabled_at is None
    assert abs_client.restored == [created.abs_user_id]


async def test_redeem_code_can_only_be_used_once_even_if_created_with_more_uses(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    redeem = await service.create_redeem_code(
        code="SHARED",
        code_type=RedeemCodeType.REGISTRATION,
        days=30,
        max_uses=10,
    )

    assert redeem.max_uses == 1

    await service.redeem_code(telegram_id=3101, code="SHARED")

    with pytest.raises(ValueError, match="已用完"):
        await service.redeem_code(telegram_id=3102, code="SHARED")


async def test_create_redeem_codes_generates_unique_single_use_codes(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    codes = await service.create_redeem_codes(
        code_type=RedeemCodeType.RENEWAL,
        days=7,
        count=3,
        created_by=9001,
    )

    assert len(codes) == 3
    assert len({code.code for code in codes}) == 3
    assert all(code.max_uses == 1 for code in codes)
    assert all(code.code.startswith("REN-") for code in codes)


async def test_create_redeem_codes_rejects_more_than_fifty(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    with pytest.raises(ValueError, match="最多生成 50 个"):
        await service.create_redeem_codes(
            code_type=RedeemCodeType.WHITELIST,
            days=None,
            count=51,
        )


async def test_expiration_prefers_active_retention_over_points(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_active_retention(enabled=True, window_days=30, extension_days=30)
    await service.set_points_renewal(enabled=True, cost_points=100, extension_days=30)
    await service.admin_adjust_points(telegram_id=4001, delta=100)
    await service.grant_registration(
        telegram_id=4001, credits=1, days=1, now=now - timedelta(days=2)
    )
    created = await service.create_account_from_registration(
        telegram_id=4001, username="dave", now=now - timedelta(days=2)
    )
    abs_client.users[created.abs_user_id]["lastSeen"] = int(
        (now - timedelta(days=1)).timestamp() * 1000
    )

    await service.process_active_renewals(now=now)
    await service.process_expiration_enforcement(now=now)
    profile = await service.get_profile(4001)

    assert profile.points == 100
    assert profile.expires_at > now
    assert abs_client.disabled == []


async def test_expiration_uses_points_then_disables_when_no_rule_applies(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_active_retention(enabled=False, window_days=30, extension_days=30)
    await service.set_points_renewal(enabled=True, cost_points=100, extension_days=30)

    await service.grant_registration(
        telegram_id=5001, credits=1, days=1, now=now - timedelta(days=2)
    )
    await service.create_account_from_registration(
        telegram_id=5001, username="erin", now=now - timedelta(days=31)
    )
    await service.admin_adjust_points(telegram_id=5001, delta=100)

    await service.grant_registration(
        telegram_id=5002, credits=1, days=1, now=now - timedelta(days=2)
    )
    poor = await service.create_account_from_registration(
        telegram_id=5002, username="frank", now=now - timedelta(days=31)
    )

    await service.process_points_renewals(now=now)
    await service.process_expiration_enforcement(now=now)
    enough_profile = await service.get_profile(5001)
    poor_profile = await service.get_profile(5002)

    assert enough_profile.points == 0
    assert enough_profile.expires_at > now
    assert poor_profile.is_disabled is True
    assert abs_client.disabled == [poor.abs_user_id]


async def test_sync_profile_activity_updates_login_and_latest_playback(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.grant_registration(telegram_id=6001, credits=1, days=30)
    created = await service.create_account_from_registration(telegram_id=6001, username="gina")
    abs_client.users[created.abs_user_id]["lastSeen"] = int(
        datetime(2026, 5, 1, tzinfo=UTC).timestamp() * 1000
    )
    abs_client.latest_playback[created.abs_user_id] = {
        "updatedAt": int(datetime(2026, 5, 10, tzinfo=UTC).timestamp() * 1000)
    }

    activity = await service.sync_profile_activity(6001)
    profile = await service.get_profile(6001)

    assert activity.last_seen_at == datetime(2026, 5, 1, tzinfo=UTC)
    assert activity.last_played_at == datetime(2026, 5, 10, tzinfo=UTC)
    assert profile.last_seen_at == datetime(2026, 5, 1, tzinfo=UTC)
    assert profile.last_played_at == datetime(2026, 5, 10, tzinfo=UTC)
    assert not hasattr(profile, "active_at")


async def test_sync_profile_activity_creates_profile_for_new_telegram_user(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)

    profile = await service.sync_profile_activity(6003)

    assert profile.telegram_id == 6003
    assert profile.abs_user_id is None


async def test_reset_password_persists_new_abs_password(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.grant_registration(telegram_id=6004, credits=1, days=30)
    await service.create_account_from_registration(telegram_id=6004, username="iris")

    result = await service.reset_password(6004)

    async with session_factory() as session:
        user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 6004))
    assert user is not None
    assert result.password
    assert user.abs_password == result.password


async def test_delete_account_clears_account_expiration_and_activity(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 11, tzinfo=UTC)
    await service.grant_registration(telegram_id=6002, credits=1, days=30, now=now)
    created = await service.create_account_from_registration(telegram_id=6002, username="hank")
    abs_client.users[created.abs_user_id]["lastSeen"] = int(
        datetime(2026, 5, 12, tzinfo=UTC).timestamp() * 1000
    )
    abs_client.latest_playback[created.abs_user_id] = {
        "updatedAt": int(datetime(2026, 5, 13, tzinfo=UTC).timestamp() * 1000)
    }
    await service.sync_profile_activity(6002)

    await service.delete_account(6002)
    profile = await service.get_profile(6002)

    assert profile.abs_user_id is None
    assert profile.abs_username is None
    assert profile.abs_password is None
    assert profile.expires_at is None
    assert profile.last_seen_at is None
    assert profile.last_played_at is None


async def test_rebind_request_approval_transfers_account_and_benefits(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
    await service.grant_registration(7001, credits=1, days=30, now=now)
    created = await service.create_account_from_registration(
        7001, "alice", now=now - timedelta(days=20)
    )
    await service.admin_adjust_points(7001, delta=88)
    await service.set_whitelist(7001, True)
    abs_client.users[created.abs_user_id]["lastSeen"] = int(
        datetime(2026, 5, 18, tzinfo=UTC).timestamp() * 1000
    )
    await service.sync_profile_activity(7001)
    abs_client.auth_users[("alice", "secret")] = {"id": created.abs_user_id, "username": "alice"}

    request = await service.create_rebind_request(7002, "alice", "secret")
    assert request.status == RebindRequestStatus.PENDING
    assert request.current_telegram_id == 7001

    reviewed = await service.approve_rebind_request(
        request.id,
        reviewer_telegram_id=9001,
        now=now,
    )
    old_profile = await service.get_profile(7001)
    new_profile = await service.get_profile(7002)

    assert reviewed.status == RebindRequestStatus.APPROVED
    assert old_profile.abs_user_id is None
    assert old_profile.abs_password is None
    assert old_profile.points == 0
    assert old_profile.is_whitelisted is False
    assert new_profile.abs_user_id == created.abs_user_id
    assert new_profile.abs_username == "alice"
    assert new_profile.abs_password == created.initial_password
    assert new_profile.points == 88
    assert new_profile.is_whitelisted is True
    assert new_profile.expires_at == now + timedelta(days=10)
    assert new_profile.last_seen_at == datetime(2026, 5, 18, tzinfo=UTC)

    with pytest.raises(ValueError, match="已处理"):
        await service.reject_rebind_request(request.id, reviewer_telegram_id=9002, now=now)


async def test_rebind_request_approval_caps_transferred_registration_credit(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)
    created = await service.create_account_from_registration(7201, "alice")
    abs_client.auth_users[("alice", "secret")] = {"id": created.abs_user_id, "username": "alice"}
    request = await service.create_rebind_request(7202, "alice", "secret")
    async with session_factory() as session:
        async with session.begin():
            current = await session.scalar(select(TgUser).where(TgUser.telegram_id == 7201))
            requester = await session.scalar(select(TgUser).where(TgUser.telegram_id == 7202))
            current.registration_credits = 1
            requester.registration_credits = 1

    await service.approve_rebind_request(request.id, reviewer_telegram_id=9001)
    new_profile = await service.get_profile(7202)

    assert new_profile.registration_credits == 1


async def test_rebind_request_rejection_does_not_change_binding(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)
    created = await service.create_account_from_registration(7101, "bob")
    abs_client.auth_users[("bob", "secret")] = {"id": created.abs_user_id, "username": "bob"}

    request = await service.create_rebind_request(7102, "bob", "secret")
    reviewed = await service.reject_rebind_request(request.id, reviewer_telegram_id=9001)
    old_profile = await service.get_profile(7101)
    new_profile = await service.get_profile(7102)

    assert reviewed.status == RebindRequestStatus.REJECTED
    assert old_profile.abs_user_id == created.abs_user_id
    assert new_profile.abs_user_id is None


async def test_points_renewal_and_expiration_enforcement_are_separate(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_active_retention(enabled=False, window_days=30, extension_days=30)
    await service.set_points_renewal(enabled=True, cost_points=100, extension_days=30)

    await service.grant_registration(
        telegram_id=5101, credits=1, days=1, now=now - timedelta(days=2)
    )
    renewed = await service.create_account_from_registration(
        telegram_id=5101, username="renewed", now=now - timedelta(days=31)
    )
    await service.admin_adjust_points(telegram_id=5101, delta=100)

    await service.grant_registration(
        telegram_id=5102, credits=1, days=1, now=now - timedelta(days=2)
    )
    disabled = await service.create_account_from_registration(
        telegram_id=5102, username="disabled", now=now - timedelta(days=31)
    )

    renewal_result = await service.process_points_renewals(now=now)
    enforcement_result = await service.process_expiration_enforcement(now=now)
    renewed_profile = await service.get_profile(5101)
    disabled_profile = await service.get_profile(5102)

    assert [item.telegram_id for item in renewal_result.points_renewed] == [5101]
    assert renewal_result.points_renewed[0].abs_username == "renewed"
    assert renewal_result.points_renewed[0].points_spent == 100
    assert renewal_result.disabled == []
    assert [item.telegram_id for item in enforcement_result.disabled] == [5102]
    assert enforcement_result.disabled[0].abs_username == "disabled"
    assert renewed_profile.is_disabled is False
    assert renewed_profile.disabled_at is None
    assert disabled_profile.is_disabled is True
    assert disabled_profile.disabled_at == now
    assert abs_client.disabled == [disabled.abs_user_id]
    assert renewed.abs_user_id not in abs_client.disabled


async def test_activity_check_result_tracks_active_retention(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_active_retention(enabled=True, window_days=30, extension_days=30)
    await service.set_points_renewal(enabled=True, cost_points=100, extension_days=30)
    await service.admin_adjust_points(telegram_id=5103, delta=100)
    await service.grant_registration(
        telegram_id=5103, credits=1, days=1, now=now - timedelta(days=2)
    )
    created = await service.create_account_from_registration(
        telegram_id=5103, username="active", now=now - timedelta(days=31)
    )
    abs_client.users[created.abs_user_id]["lastSeen"] = int(
        (now - timedelta(days=1)).timestamp() * 1000
    )

    result = await service.process_active_renewals(now=now)
    profile = await service.get_profile(5103)

    assert [item.telegram_id for item in result.active_renewed] == [5103]
    assert result.active_renewed[0].abs_username == "active"
    assert profile.points == 100
    assert profile.expires_at == now + timedelta(days=30)


async def test_active_renewal_does_not_disable_inactive_users(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_active_retention(enabled=True, window_days=30, extension_days=30)
    await service.grant_registration(telegram_id=5105, credits=1, days=30, now=now)
    created = await service.create_account_from_registration(
        telegram_id=5105, username="inactive", now=now
    )
    abs_client.users[created.abs_user_id]["lastSeen"] = int(
        (now - timedelta(days=31)).timestamp() * 1000
    )

    result = await service.process_active_renewals(now=now)
    profile = await service.get_profile(5105)

    assert result.disabled == []
    assert result.deleted == []
    assert profile.is_disabled is False
    assert abs_client.disabled == []


async def test_activity_check_does_not_shorten_longer_expiration(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    longer_expiration = now + timedelta(days=365)
    await service.set_active_retention(enabled=True, window_days=30, extension_days=30)
    await service.grant_registration(telegram_id=5104, credits=1, days=365, now=now)
    created = await service.create_account_from_registration(
        telegram_id=5104, username="longactive", now=now
    )
    abs_client.users[created.abs_user_id]["lastSeen"] = int(
        (now - timedelta(days=1)).timestamp() * 1000
    )

    result = await service.process_active_renewals(now=now)
    profile = await service.get_profile(5104)

    assert result.active_renewed == []
    assert profile.expires_at == longer_expiration


async def test_disabled_delete_after_zero_keeps_disabled_account(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_system_settings(disabled_delete_after_days=0)
    await service.set_active_retention(enabled=False, window_days=30, extension_days=30)
    await service.set_points_renewal(enabled=False, cost_points=100, extension_days=30)
    await service.grant_registration(
        telegram_id=5201, credits=1, days=1, now=now - timedelta(days=5)
    )
    created = await service.create_account_from_registration(
        telegram_id=5201, username="keep", now=now - timedelta(days=31)
    )

    first = await service.process_expiration_enforcement(now=now)
    second = await service.process_expiration_enforcement(now=now + timedelta(days=30))
    profile = await service.get_profile(5201)

    assert [item.telegram_id for item in first.disabled] == [5201]
    assert second.deleted == []
    assert profile.abs_user_id == created.abs_user_id
    assert profile.abs_username == "keep"
    assert profile.is_disabled is True
    assert profile.disabled_at == now
    assert abs_client.deleted == []


async def test_expiration_enforcement_disabled_skips_disable_and_delete(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_expiration_enforcement(enabled=False)
    await service.grant_registration(
        telegram_id=5206, credits=1, days=1, now=now - timedelta(days=5)
    )
    created = await service.create_account_from_registration(
        telegram_id=5206, username="expired", now=now - timedelta(days=31)
    )

    result = await service.process_expiration_enforcement(now=now)
    profile = await service.get_profile(5206)

    assert result.disabled == []
    assert result.deleted == []
    assert profile.is_disabled is False
    assert profile.abs_user_id == created.abs_user_id
    assert abs_client.disabled == []
    assert abs_client.deleted == []


async def test_disabled_delete_after_days_deletes_old_disabled_account(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_system_settings(disabled_delete_after_days=3)
    await service.set_active_retention(enabled=False, window_days=30, extension_days=30)
    await service.set_points_renewal(enabled=False, cost_points=100, extension_days=30)
    await service.grant_registration(
        telegram_id=5202, credits=1, days=1, now=now - timedelta(days=5)
    )
    created = await service.create_account_from_registration(
        telegram_id=5202, username="delete", now=now - timedelta(days=31)
    )

    await service.process_expiration_enforcement(now=now)
    result = await service.process_expiration_enforcement(now=now + timedelta(days=3))
    profile = await service.get_profile(5202)

    assert [item.telegram_id for item in result.deleted] == [5202]
    assert result.deleted[0].abs_username == "delete"
    assert profile.abs_user_id is None
    assert profile.abs_username is None
    assert profile.abs_password is None
    assert profile.is_disabled is False
    assert profile.disabled_at is None
    assert abs_client.deleted == [created.abs_user_id]


async def test_disabled_delete_after_days_deletes_legacy_disabled_account_with_updated_at(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    disabled_since = now - timedelta(days=3)
    await service.set_system_settings(disabled_delete_after_days=3)
    abs_client.users["usr_legacy"] = {
        "id": "usr_legacy",
        "username": "legacy",
        "lastSeen": int((now - timedelta(hours=1)).timestamp() * 1000),
    }
    async with session_factory() as session:
        async with session.begin():
            session.add(
                TgUser(
                    telegram_id=5204,
                    abs_user_id="usr_legacy",
                    abs_username="legacy",
                    abs_password="legacy-secret",
                    is_disabled=True,
                    disabled_at=None,
                    updated_at=disabled_since,
                )
            )

    result = await service.process_expiration_enforcement(now=now)
    profile = await service.get_profile(5204)

    assert [item.telegram_id for item in result.deleted] == [5204]
    assert result.deleted[0].disabled_at == disabled_since
    assert profile.abs_user_id is None
    assert profile.abs_username is None
    assert profile.abs_password is None
    assert profile.is_disabled is False
    assert profile.disabled_at is None
    assert abs_client.deleted == ["usr_legacy"]


async def test_expiration_enforcement_auto_delete_clears_abs_password(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_system_settings(disabled_delete_after_days=3)
    abs_client.users["usr_inactive"] = {
        "id": "usr_inactive",
        "username": "inactive",
        "lastSeen": int((now - timedelta(days=10)).timestamp() * 1000),
    }
    async with session_factory() as session:
        async with session.begin():
            session.add(
                TgUser(
                    telegram_id=5205,
                    abs_user_id="usr_inactive",
                    abs_username="inactive",
                    abs_password="inactive-secret",
                    is_disabled=True,
                    disabled_at=now - timedelta(days=3),
                )
            )

    result = await service.process_expiration_enforcement(now=now)
    profile = await service.get_profile(5205)

    assert [item.telegram_id for item in result.deleted] == [5205]
    assert profile.abs_user_id is None
    assert profile.abs_username is None
    assert profile.abs_password is None
    assert profile.is_disabled is False
    assert abs_client.deleted == ["usr_inactive"]


async def test_disabled_delete_after_days_keeps_whitelisted_disabled_account(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
    await service.set_system_settings(disabled_delete_after_days=3)
    await service.set_active_retention(enabled=False, window_days=30, extension_days=30)
    await service.set_points_renewal(enabled=False, cost_points=100, extension_days=30)
    await service.grant_registration(
        telegram_id=5203, credits=1, days=1, now=now - timedelta(days=5)
    )
    created = await service.create_account_from_registration(
        telegram_id=5203, username="white", now=now - timedelta(days=31)
    )

    await service.process_expiration_enforcement(now=now)
    async with session_factory() as session:
        async with session.begin():
            user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 5203))
            user.is_whitelisted = True
            user.disabled_at = None
            user.updated_at = now
    result = await service.process_expiration_enforcement(now=now + timedelta(days=3))
    profile = await service.get_profile(5203)

    assert result.deleted == []
    assert profile.abs_user_id == created.abs_user_id
    assert profile.abs_username == "white"
    assert profile.is_whitelisted is True
    assert profile.is_disabled is True
    assert profile.disabled_at == now
    assert abs_client.deleted == []


async def test_set_points_unban_persists_settings(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    await service.set_points_unban(enabled=True, cost_points=50)
    settings = await service.get_public_settings()

    assert settings.points_unban_enabled is True
    assert settings.points_unban_cost_points == 50


async def test_set_points_unban_clamps_cost_to_minimum_1(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    await service.set_points_unban(enabled=True, cost_points=0)
    settings = await service.get_public_settings()

    assert settings.points_unban_cost_points == 1


async def test_self_unban_by_points_deducts_points_and_clears_disabled(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_points_unban(enabled=True, cost_points=80)
    # 创建一个被禁用的用户
    await service.get_profile(9001)
    async with session_factory() as session:
        async with session.begin():
            user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 9001))
            user.abs_user_id = "usr_banned"
            user.points = 200
            user.is_disabled = True
    abs_client.users["usr_banned"] = {"id": "usr_banned"}

    await service.self_unban_by_points(9001)

    profile = await service.get_profile(9001)
    assert profile.is_disabled is False
    assert profile.points == 120  # 200 - 80
    assert "usr_banned" in abs_client.restored


async def test_self_unban_by_points_raises_when_insufficient(session_factory, abs_client):
    from absbot.service import InsufficientPointsError

    service = MembershipService(session_factory, abs_client)
    await service.set_points_unban(enabled=True, cost_points=150)
    await service.get_profile(9002)  # ensure user exists
    async with session_factory() as session:
        async with session.begin():
            user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 9002))
            user.is_disabled = True
            user.points = 50

    with pytest.raises(InsufficientPointsError) as exc_info:
        await service.self_unban_by_points(9002)

    err = exc_info.value
    assert err.current == 50
    assert err.needed == 150
    assert err.deficit == 100


async def test_self_unban_by_points_raises_when_feature_disabled(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_points_unban(enabled=False, cost_points=100)
    await service.get_profile(9003)  # ensure user exists
    async with session_factory() as session:
        async with session.begin():
            user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 9003))
            user.is_disabled = True
            user.points = 999

    with pytest.raises(ValueError, match="未开启"):
        await service.self_unban_by_points(9003)


async def test_sync_users_to_abs_syncs_active_states(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=2)
    r1 = await service.create_account_from_registration(telegram_id=1001, username="alice")
    r2 = await service.create_account_from_registration(telegram_id=1002, username="bob")
    # 在 bot DB 中禁用 bob
    async with session_factory() as sess:
        from sqlalchemy import select as sa_select

        bob = (await sess.execute(sa_select(TgUser).where(TgUser.telegram_id == 1002))).scalar_one()
        bob.is_disabled = True
        await sess.commit()

    result = await service.sync_users_to_abs()

    assert isinstance(result, SyncResult)
    assert result.synced_count == 2
    assert result.failed_count == 0
    assert result.recreated == []
    assert r2.abs_user_id in abs_client.disabled
    assert r1.abs_user_id in abs_client.restored


async def test_sync_users_to_abs_recreates_missing_abs_user(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)
    r = await service.create_account_from_registration(telegram_id=1003, username="carol")
    # 从 ABS fake 中移除用户，模拟 ABS 端丢失
    del abs_client.users[r.abs_user_id]

    result = await service.sync_users_to_abs()

    assert result.synced_count == 0
    assert result.failed_count == 0
    assert len(result.recreated) == 1
    tg_id, username, password = result.recreated[0]
    assert tg_id == 1003
    assert username == "carol"
    assert len(password) > 0


async def test_sync_users_to_abs_relinks_existing_username_with_new_abs_id(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                TgUser(
                    telegram_id=1005,
                    abs_user_id="old_abs_id",
                    abs_username="zz",
                    abs_password="old-password",
                )
            )
    abs_client.users["new_abs_id"] = {
        "id": "new_abs_id",
        "username": "zz",
        "lastSeen": None,
        "isActive": True,
    }

    result = await service.sync_users_to_abs()

    async with session_factory() as session:
        user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 1005))
    assert result.synced_count == 1
    assert result.failed_count == 0
    assert result.recreated == []
    assert user.abs_user_id == "new_abs_id"
    assert "new_abs_id" in abs_client.restored


async def test_reset_password_relinks_abs_user_id_after_not_found(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    async with session_factory() as session:
        async with session.begin():
            session.add(TgUser(telegram_id=1006, abs_user_id="old_abs_id", abs_username="zz"))
    abs_client.users["new_abs_id"] = {
        "id": "new_abs_id",
        "username": "zz",
        "lastSeen": None,
        "isActive": True,
    }

    async def reset_password(abs_user_id, password):
        if abs_user_id == "old_abs_id":
            from absbot.abs_client import AudiobookshelfNotFoundError

            raise AudiobookshelfNotFoundError("用户不存在")
        abs_client.reset.append((abs_user_id, password))
        return {"id": abs_user_id}

    abs_client.reset_password = reset_password

    result = await service.reset_password(1006)

    async with session_factory() as session:
        user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 1006))
    assert result.username == "zz"
    assert user.abs_user_id == "new_abs_id"
    assert abs_client.reset[0][0] == "new_abs_id"


async def test_process_expiration_enforcement_relinks_abs_user_id_before_disabling(
    session_factory, abs_client
):
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 22, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                TgUser(
                    telegram_id=1007,
                    abs_user_id="old_abs_id",
                    abs_username="zz",
                    expires_at=now - timedelta(days=1),
                )
            )
    abs_client.users["new_abs_id"] = {
        "id": "new_abs_id",
        "username": "zz",
        "lastSeen": None,
        "isActive": True,
    }

    async def get_user(abs_user_id):
        if abs_user_id == "old_abs_id":
            from absbot.abs_client import AudiobookshelfNotFoundError

            raise AudiobookshelfNotFoundError("用户不存在")
        return abs_client.users[abs_user_id]

    async def disable_user(abs_user_id):
        if abs_user_id == "old_abs_id":
            from absbot.abs_client import AudiobookshelfNotFoundError

            raise AudiobookshelfNotFoundError("用户不存在")
        abs_client.disabled.append(abs_user_id)

    abs_client.get_user = get_user
    abs_client.disable_user = disable_user

    result = await service.process_expiration_enforcement(now=now)

    async with session_factory() as session:
        user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 1007))
    assert user.abs_user_id == "new_abs_id"
    assert result.disabled[0].abs_user_id == "new_abs_id"
    assert abs_client.disabled == ["new_abs_id"]


async def test_sync_users_to_abs_counts_failed_on_error(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)
    await service.set_registration(opened=True, slots=1)
    await service.create_account_from_registration(telegram_id=1004, username="dave")

    # 让 restore_user 抛异常
    from absbot.abs_client import AudiobookshelfError

    async def bad_restore(uid):
        raise AudiobookshelfError("模拟失败")

    abs_client.restore_user = bad_restore

    result = await service.sync_users_to_abs()

    assert result.failed_count == 1
    assert result.synced_count == 0


# ── get_total_book_count ───────────────────────────────────────────────────────


@pytest.fixture
def mock_abs_client():
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.get_libraries = AsyncMock()
    client.get_library_stats = AsyncMock()
    return client


@pytest.fixture
def service(session_factory, mock_abs_client):
    from absbot.service import MembershipService

    return MembershipService(session_factory, mock_abs_client)


@pytest.mark.anyio
async def test_get_total_book_count_sums_all_libraries(service, mock_abs_client):
    mock_abs_client.get_libraries.return_value = [
        {"id": "lib_1"},
        {"id": "lib_2"},
    ]

    async def get_stats(lib_id):
        if lib_id == "lib_1":
            return {"totalItems": 150}
        elif lib_id == "lib_2":
            return {"totalItems": 30}
        return {}

    mock_abs_client.get_library_stats.side_effect = get_stats
    count = await service.get_total_book_count()
    assert count == 180


@pytest.mark.anyio
async def test_get_total_book_count_returns_zero_for_empty_libraries(service, mock_abs_client):
    mock_abs_client.get_libraries.return_value = []
    count = await service.get_total_book_count()
    assert count == 0


@pytest.mark.anyio
async def test_get_total_book_count_returns_none_on_error(service, mock_abs_client):
    mock_abs_client.get_libraries.side_effect = AudiobookshelfError("network error")
    count = await service.get_total_book_count()
    assert count is None

    mock_abs_client.get_libraries.side_effect = None
    mock_abs_client.get_libraries.return_value = [{"id": "lib_1"}]
    mock_abs_client.get_library_stats.side_effect = AudiobookshelfError("network error")
    count = await service.get_total_book_count()
    assert count is None


@pytest.mark.anyio
async def test_get_total_book_count_handles_missing_stats(service, mock_abs_client):
    mock_abs_client.get_libraries.return_value = [
        {"id": "lib_1"},
        {"id": "lib_2"},
    ]

    async def get_stats(lib_id):
        return {}

    mock_abs_client.get_library_stats.side_effect = get_stats
    count = await service.get_total_book_count()
    assert count == 0


# ---------------------------------------------------------------------------
# renewal_days tests
# ---------------------------------------------------------------------------


async def test_redeem_registration_code_stores_renewal_days_not_expires_at(
    session_factory, abs_client
):
    """Registration code redemption should set renewal_days but NOT set expires_at."""
    service = MembershipService(session_factory, abs_client)
    await service.create_redeem_code(
        code="REG60",
        code_type=RedeemCodeType.REGISTRATION,
        days=60,
        max_uses=1,
    )

    result = await service.redeem_code(telegram_id=8001, code="REG60")
    profile = await service.get_profile(8001)

    assert result.registration_credits == 1
    assert profile.renewal_days == 60
    assert profile.expires_at is None


async def test_redeem_registration_code_without_days_leaves_renewal_days_none(
    session_factory, abs_client
):
    """Registration code with days=None should not set renewal_days."""
    service = MembershipService(session_factory, abs_client)
    await service.create_redeem_code(
        code="REGNULL",
        code_type=RedeemCodeType.REGISTRATION,
        days=None,
        max_uses=1,
    )

    await service.redeem_code(telegram_id=8002, code="REGNULL")
    profile = await service.get_profile(8002)

    assert profile.registration_credits == 1
    assert profile.renewal_days is None


async def test_create_account_uses_renewal_days_over_default(session_factory, abs_client):
    """Account creation should use renewal_days instead of default_register_days."""
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
    await service.create_redeem_code(
        code="REG90",
        code_type=RedeemCodeType.REGISTRATION,
        days=90,
        max_uses=1,
    )
    await service.redeem_code(telegram_id=8003, code="REG90")

    created = await service.create_account_from_registration(
        telegram_id=8003, username="alice90", now=now
    )

    assert created.expires_at == now + timedelta(days=90)


async def test_create_account_falls_back_to_default_register_days_when_renewal_days_is_none(
    session_factory, abs_client
):
    """When renewal_days is None, account creation falls back to default_register_days."""
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
    await service.set_system_settings(default_register_days=45)
    await service.create_redeem_code(
        code="REGNONE",
        code_type=RedeemCodeType.REGISTRATION,
        days=None,
        max_uses=1,
    )
    await service.redeem_code(telegram_id=8004, code="REGNONE")

    created = await service.create_account_from_registration(
        telegram_id=8004, username="bob45", now=now
    )

    assert created.expires_at == now + timedelta(days=45)


async def test_create_account_uses_extend_from_when_expires_at_in_future(
    session_factory, abs_client
):
    """If user already has a future expires_at, account creation extends from that."""
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
    future_expires = now + timedelta(days=10)
    await service.create_redeem_code(
        code="REG30EXT",
        code_type=RedeemCodeType.REGISTRATION,
        days=30,
        max_uses=1,
    )
    await service.redeem_code(telegram_id=8005, code="REG30EXT")
    # Manually set a future expires_at via admin
    await service.admin_adjust_expiry(8005, delta=10, now=now)

    created = await service.create_account_from_registration(
        telegram_id=8005, username="ext_user", now=now
    )

    # Should extend from the future expires_at, not from now
    assert created.expires_at == future_expires + timedelta(days=30)


async def test_create_account_uses_now_when_expires_at_in_past(session_factory, abs_client):
    """If user has an expired expires_at, account creation extends from now."""
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
    past_expires = now - timedelta(days=5)
    await service.create_redeem_code(
        code="REG30PAST",
        code_type=RedeemCodeType.REGISTRATION,
        days=30,
        max_uses=1,
    )
    await service.redeem_code(telegram_id=8006, code="REG30PAST")
    # Manually set a past expires_at
    async with session_factory() as session:
        async with session.begin():
            user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 8006))
            user.expires_at = past_expires

    created = await service.create_account_from_registration(
        telegram_id=8006, username="past_user", now=now
    )

    # Should extend from now since expires_at is in the past
    assert created.expires_at == now + timedelta(days=30)


async def test_bind_existing_account_uses_renewal_days(session_factory, abs_client):
    """bind_existing_account should use renewal_days when available."""
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
    abs_client.auth_users[("alice", "secret")] = {"id": "usr_bind", "username": "alice"}
    # Set renewal_days manually for the user
    async with session_factory() as session:
        async with session.begin():
            user = TgUser(telegram_id=8007, renewal_days=60)
            session.add(user)

    result = await service.bind_existing_account(8007, "alice", "secret", now=now)

    assert result.expires_at == now + timedelta(days=60)


async def test_approve_rebind_uses_renewal_days(session_factory, abs_client):
    """approve_rebind_request should use requester's renewal_days."""
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
    await service.set_registration(opened=True, slots=1)
    created = await service.create_account_from_registration(8010, "alice", now=now)
    abs_client.auth_users[("alice", "secret")] = {"id": created.abs_user_id, "username": "alice"}

    # Create requester with custom renewal_days
    async with session_factory() as session:
        async with session.begin():
            requester = TgUser(telegram_id=8011, renewal_days=90)
            session.add(requester)

    request = await service.create_rebind_request(8011, "alice", "secret", now=now)
    await service.approve_rebind_request(request.id, reviewer_telegram_id=9001, now=now)

    new_profile = await service.get_profile(8011)
    assert new_profile.expires_at == now + timedelta(days=90)


async def test_transfer_account_transfers_renewal_days(session_factory, abs_client):
    """_transfer_account_ownership should transfer renewal_days from current to requester."""
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
    await service.create_redeem_code(
        code="REG75",
        code_type=RedeemCodeType.REGISTRATION,
        days=75,
        max_uses=1,
    )
    await service.redeem_code(telegram_id=8020, code="REG75")
    created = await service.create_account_from_registration(8020, "transfer_src", now=now)
    abs_client.auth_users[("transfer_src", "secret")] = {
        "id": created.abs_user_id,
        "username": "transfer_src",
    }

    request = await service.create_rebind_request(8021, "transfer_src", "secret", now=now)
    await service.approve_rebind_request(request.id, reviewer_telegram_id=9001, now=now)

    old_profile = await service.get_profile(8020)
    new_profile = await service.get_profile(8021)

    assert old_profile.renewal_days is None
    assert new_profile.renewal_days is None


async def test_transfer_account_keeps_requester_renewal_days_if_set(session_factory, abs_client):
    """If requester already has renewal_days, it should be kept over the transferred value."""
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
    await service.set_registration(opened=True, slots=1)
    created = await service.create_account_from_registration(8030, "keep_src", now=now)
    abs_client.auth_users[("keep_src", "secret")] = {
        "id": created.abs_user_id,
        "username": "keep_src",
    }
    # Set renewal_days on current user
    async with session_factory() as session:
        async with session.begin():
            user = await session.scalar(select(TgUser).where(TgUser.telegram_id == 8030))
            user.renewal_days = 50

    # Create requester with their own renewal_days
    async with session_factory() as session:
        async with session.begin():
            requester = TgUser(telegram_id=8031, renewal_days=120)
            session.add(requester)

    request = await service.create_rebind_request(8031, "keep_src", "secret", now=now)
    await service.approve_rebind_request(request.id, reviewer_telegram_id=9001, now=now)

    new_profile = await service.get_profile(8031)
    # Requester's own renewal_days should be used to extend expires_at and then cleared
    assert new_profile.expires_at == now + timedelta(days=120)
    assert new_profile.renewal_days is None


async def test_grant_registration_without_days_uses_default_register_days(
    session_factory, abs_client
):
    """If grant_registration is called with credits=1 and days=None for a user without ABS account,
    renewal_days should be set to system.default_register_days."""
    service = MembershipService(session_factory, abs_client)
    await service.set_system_settings(default_register_days=45)

    await service.grant_registration(telegram_id=8040, credits=1, days=None)
    profile = await service.get_profile(8040)

    assert profile.registration_credits == 1
    assert profile.renewal_days == 45


async def test_redeem_renewal_code_without_account_adds_to_renewal_days(
    session_factory, abs_client
):
    """If a user has no ABS account, redeeming a renewal code should add its days to renewal_days."""
    service = MembershipService(session_factory, abs_client)
    now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)

    await service.create_redeem_code(
        code="REN15",
        code_type=RedeemCodeType.RENEWAL,
        days=15,
        max_uses=1,
    )

    await service.grant_registration(telegram_id=8050, credits=1, days=30)

    result = await service.redeem_code(telegram_id=8050, code="REN15")
    assert "已增加续期天数" in result.message

    profile = await service.get_profile(8050)
    assert profile.renewal_days == 45
    assert profile.expires_at is None

    created = await service.create_account_from_registration(
        telegram_id=8050, username="user8050", now=now
    )
    assert created.expires_at == now + timedelta(days=45)


async def test_redeem_renewal_code_without_account_adds_to_default_renewal_days(
    session_factory, abs_client
):
    """If a user has no ABS account and renewal_days is None, redeeming a renewal code adds to default_register_days."""
    service = MembershipService(session_factory, abs_client)

    await service.set_system_settings(default_register_days=30)
    await service.create_redeem_code(
        code="REN20",
        code_type=RedeemCodeType.RENEWAL,
        days=20,
        max_uses=1,
    )

    # User gets registration credits without specific days
    await service.grant_registration(telegram_id=8060, credits=1, days=None)

    await service.redeem_code(telegram_id=8060, code="REN20")
    profile = await service.get_profile(8060)
    assert profile.renewal_days == 50


async def test_list_redeem_codes_usable_filter(session_factory, abs_client):
    from absbot.models import RedeemCode

    service = MembershipService(session_factory, abs_client)
    now = datetime.now(timezone.utc)

    # 1. Usable code (no expiration, unused, active)
    await service.create_redeem_code(
        code="USABLE1", code_type=RedeemCodeType.REGISTRATION, days=30, max_uses=1
    )
    # 2. Usable code (future expiration, unused, active)
    c2 = await service.create_redeem_code(
        code="USABLE2", code_type=RedeemCodeType.REGISTRATION, days=30, max_uses=1
    )
    async with session_factory() as session:
        async with session.begin():
            db_c2 = await session.get(RedeemCode, c2.id)
            db_c2.expires_at = now + timedelta(days=1)

    # 3. Unusable (inactive)
    c3 = await service.create_redeem_code(
        code="UNUSABLE_INACTIVE", code_type=RedeemCodeType.REGISTRATION, days=30, max_uses=1
    )
    await service.set_redeem_code_active(c3.id, False)

    # 4. Unusable (fully used)
    c4 = await service.create_redeem_code(
        code="UNUSABLE_USED", code_type=RedeemCodeType.REGISTRATION, days=30, max_uses=1
    )
    async with session_factory() as session:
        async with session.begin():
            db_c4 = await session.get(RedeemCode, c4.id)
            db_c4.used_count = 1

    # 5. Unusable (expired)
    c5 = await service.create_redeem_code(
        code="UNUSABLE_EXPIRED", code_type=RedeemCodeType.REGISTRATION, days=30, max_uses=1
    )
    async with session_factory() as session:
        async with session.begin():
            db_c5 = await session.get(RedeemCode, c5.id)
            db_c5.expires_at = now - timedelta(days=1)

    # Verify standard listing returns all 5
    all_codes = await service.list_redeem_codes(code_type=RedeemCodeType.REGISTRATION, limit=100)
    assert len(all_codes) == 5

    # Verify usable list only returns the 2 usable ones
    usable_codes = await service.list_redeem_codes(
        code_type=RedeemCodeType.REGISTRATION, usable=True, limit=100
    )
    assert len(usable_codes) == 2
    codes_set = {c.code for c in usable_codes}
    assert codes_set == {"USABLE1", "USABLE2"}


async def test_get_user_counts(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    # 1. Verify initially 0
    db_count, abs_count = await service.get_user_counts()
    assert db_count == 0
    assert abs_count == 0

    # 2. Add some users in DB (one with account, one without account)
    async with session_factory() as session:
        async with session.begin():
            session.add(TgUser(telegram_id=1111, abs_user_id="usr_1111", abs_username="user1"))
            session.add(TgUser(telegram_id=2222, abs_user_id=None, abs_username=None))

    db_count, abs_count = await service.get_user_counts()
    assert db_count == 1
    assert abs_count == 0

    # 3. Add users to ABS client
    abs_client.users["usr_1111"] = {"id": "usr_1111", "username": "user1"}
    abs_client.users["usr_3333"] = {"id": "usr_3333", "username": "user3"}

    db_count, abs_count = await service.get_user_counts()
    assert db_count == 1
    assert abs_count == 2

    # 4. Mock client error
    async def bad_list_users():
        raise RuntimeError("ABS down")

    abs_client.list_users = bad_list_users

    db_count, abs_count = await service.get_user_counts()
    assert db_count == 1
    assert abs_count is None


async def test_get_user_counts_extended(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    # 1. 初始状态
    db_count, abs_count, unbound_count = await service.get_user_counts_extended()
    assert db_count == 0
    assert abs_count == 0
    assert unbound_count == 0

    # 2. 插入用户数据
    async with session_factory() as session:
        async with session.begin():
            # 绑定了 bot
            session.add(TgUser(telegram_id=111, abs_user_id="usr_111", abs_username="user1"))
            # 未绑定 bot，但在数据库有记录 (abs_user_id is None)
            session.add(TgUser(telegram_id=222, abs_user_id=None, abs_username=None))

    # ABS 上有：绑定了bot的用户，未绑定的独立用户，以及 root 管理员
    abs_client.users["usr_111"] = {"id": "usr_111", "username": "user1", "type": "user"}
    abs_client.users["usr_222"] = {"id": "usr_222", "username": "user2", "type": "user"} # 未绑定
    abs_client.users["usr_root"] = {"id": "usr_root", "username": "admin", "type": "root"} # root

    db_count, abs_count, unbound_count = await service.get_user_counts_extended()
    assert db_count == 1
    assert abs_count == 3
    assert unbound_count == 1


async def test_delete_all_bot_users(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    async with session_factory() as session:
        async with session.begin():
            # 绑定了 bot 1 (普通用户)
            session.add(TgUser(telegram_id=111, abs_user_id="usr_111", abs_username="user1"))
            # 绑定了 bot 2 (假装这个是 root 用户)
            session.add(TgUser(telegram_id=222, abs_user_id="usr_root", abs_username="admin"))
            # 未绑定 bot 的 TgUser
            session.add(TgUser(telegram_id=333, abs_user_id=None, abs_username=None))

    abs_client.users["usr_111"] = {"id": "usr_111", "username": "user1", "type": "user"}
    abs_client.users["usr_root"] = {"id": "usr_root", "username": "admin", "type": "root"}

    # 动态支持删除
    async def custom_delete_user(abs_user_id: str):
        abs_client.deleted.append(abs_user_id)
        abs_client.users.pop(abs_user_id, None)
    abs_client.delete_user = custom_delete_user

    deleted_count = await service.delete_all_bot_users()
    
    assert deleted_count == 1
    assert "usr_111" in abs_client.deleted
    assert "usr_root" not in abs_client.deleted

    # 检查数据库中 usr_111 被清除了，而 usr_root (tg_id 222) 和未绑定用户 (tg_id 333) 还留着
    async with session_factory() as session:
        user_111 = await session.scalar(select(TgUser).where(TgUser.telegram_id == 111))
        user_222 = await session.scalar(select(TgUser).where(TgUser.telegram_id == 222))
        user_333 = await session.scalar(select(TgUser).where(TgUser.telegram_id == 333))
    assert user_111 is None
    assert user_222 is not None
    assert user_333 is not None


async def test_delete_unbound_abs_users(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    async with session_factory() as session:
        async with session.begin():
            # 绑定了 bot 的用户
            session.add(TgUser(telegram_id=111, abs_user_id="usr_111", abs_username="user1"))

    abs_client.users["usr_111"] = {"id": "usr_111", "username": "user1", "type": "user"}
    abs_client.users["usr_222"] = {"id": "usr_222", "username": "user2", "type": "user"} # 未绑定
    abs_client.users["usr_root"] = {"id": "usr_root", "username": "admin", "type": "root"} # root

    # 动态支持删除
    async def custom_delete_user(abs_user_id: str):
        abs_client.deleted.append(abs_user_id)
        abs_client.users.pop(abs_user_id, None)
    abs_client.delete_user = custom_delete_user

    deleted_count = await service.delete_unbound_abs_users()
    
    # 只有 usr_222 应该被删除，usr_root 排除，usr_111 排除
    assert deleted_count == 1
    assert "usr_222" in abs_client.deleted
    assert "usr_111" not in abs_client.deleted
    assert "usr_root" not in abs_client.deleted


async def test_clear_all_users(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    async with session_factory() as session:
        async with session.begin():
            # 绑定了 bot 的用户
            session.add(TgUser(telegram_id=111, abs_user_id="usr_111", abs_username="user1"))
            # 未绑定 bot 的 TgUser 记录
            session.add(TgUser(telegram_id=222, abs_user_id=None, abs_username=None))

    abs_client.users["usr_111"] = {"id": "usr_111", "username": "user1", "type": "user"}
    abs_client.users["usr_222"] = {"id": "usr_222", "username": "user2", "type": "user"} # 独立的，未绑定
    abs_client.users["usr_root"] = {"id": "usr_root", "username": "admin", "type": "root"} # root

    # 动态支持删除
    async def custom_delete_user(abs_user_id: str):
        abs_client.deleted.append(abs_user_id)
        abs_client.users.pop(abs_user_id, None)
    abs_client.delete_user = custom_delete_user

    deleted_abs, deleted_db = await service.clear_all_users()
    
    # 应删除 usr_111, usr_222。排除 usr_root。
    assert deleted_abs == 2
    assert "usr_111" in abs_client.deleted
    assert "usr_222" in abs_client.deleted
    assert "usr_root" not in abs_client.deleted

    # 数据库记录应该被全部清空
    assert deleted_db == 2
    async with session_factory() as session:
        db_count = await session.scalar(select(func.count()).select_from(TgUser))
    assert db_count == 0


async def test_leaderboard_push_settings(session_factory, abs_client):
    service = MembershipService(session_factory, abs_client)

    # Check defaults
    public = await service.get_public_settings()
    assert public.daily_leaderboard_enabled is True
    assert public.weekly_leaderboard_enabled is True

    # Toggle daily setting
    updated = await service.set_daily_leaderboard_push(enabled=False)
    assert updated.daily_leaderboard_enabled is False
    assert updated.weekly_leaderboard_enabled is True

    # Toggle weekly setting
    updated = await service.set_weekly_leaderboard_push(enabled=False)
    assert updated.daily_leaderboard_enabled is False
    assert updated.weekly_leaderboard_enabled is False

    # Retrieve from DB and verify
    public2 = await service.get_public_settings()
    assert public2.daily_leaderboard_enabled is False
    assert public2.weekly_leaderboard_enabled is False

    # Turn daily back on
    updated = await service.set_daily_leaderboard_push(enabled=True)
    assert updated.daily_leaderboard_enabled is True
    assert updated.weekly_leaderboard_enabled is False

