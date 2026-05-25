from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from absbot.backup import _escape_value, cleanup_old_backups, list_local_backups


def test_list_local_backups_returns_newest_first(tmp_path: Path) -> None:
    for name in ["backup_20260520_050000.sql", "backup_20260522_050000.sql", "backup_20260521_050000.sql"]:
        (tmp_path / name).write_text("-- test")

    result = list_local_backups(str(tmp_path))

    assert [p.name for p in result] == [
        "backup_20260522_050000.sql",
        "backup_20260521_050000.sql",
        "backup_20260520_050000.sql",
    ]


def test_list_local_backups_empty_dir(tmp_path: Path) -> None:
    assert list_local_backups(str(tmp_path)) == []


def test_list_local_backups_creates_dir_if_missing(tmp_path: Path) -> None:
    missing = str(tmp_path / "backups")
    result = list_local_backups(missing)
    assert result == []
    assert Path(missing).exists()


def test_cleanup_old_backups_removes_oldest(tmp_path: Path) -> None:
    for name in ["backup_20260520_050000.sql", "backup_20260521_050000.sql", "backup_20260522_050000.sql"]:
        (tmp_path / name).write_text("-- test")

    deleted = cleanup_old_backups(str(tmp_path), keep_count=2)

    assert deleted == 1
    remaining = {p.name for p in tmp_path.iterdir()}
    assert "backup_20260520_050000.sql" not in remaining
    assert "backup_20260521_050000.sql" in remaining
    assert "backup_20260522_050000.sql" in remaining


def test_cleanup_old_backups_does_nothing_when_under_limit(tmp_path: Path) -> None:
    (tmp_path / "backup_20260520_050000.sql").write_text("-- test")

    deleted = cleanup_old_backups(str(tmp_path), keep_count=7)

    assert deleted == 0
    assert len(list(tmp_path.iterdir())) == 1


def test_cleanup_old_backups_keep_count_zero_removes_all(tmp_path: Path) -> None:
    for name in ["backup_20260520_050000.sql", "backup_20260521_050000.sql"]:
        (tmp_path / name).write_text("-- test")

    deleted = cleanup_old_backups(str(tmp_path), keep_count=0)

    assert deleted == 2
    assert list(tmp_path.iterdir()) == []


def test_escape_value_none() -> None:
    assert _escape_value(None) == "NULL"


def test_escape_value_bool() -> None:
    assert _escape_value(True) == "1"
    assert _escape_value(False) == "0"


def test_escape_value_int() -> None:
    assert _escape_value(42) == "42"


def test_escape_value_string_escapes_special_chars() -> None:
    assert _escape_value("it's") == "'it\\'s'"
    assert _escape_value("line\nnew") == "'line\\nnew'"
    assert _escape_value("back\\slash") == "'back\\\\slash'"


def test_escape_value_datetime() -> None:
    dt = datetime(2026, 5, 22, 5, 0, 0, tzinfo=timezone.utc)
    result = _escape_value(dt)
    assert result.startswith("'2026-05-22")


def test_escape_value_bytes() -> None:
    assert _escape_value(b"\xde\xad") == "X'dead'"


async def test_dump_and_restore_roundtrip(tmp_path: Path) -> None:
    """dump_database + restore_database 往返测试（SQLite in-memory）。"""
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import create_async_engine
    from absbot.models import Base, BotSetting, RegistrationQueue, RegistrationQueueStatus

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 写入一条设置
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        async with sess.begin():
            sess.add_all(
                [
                    BotSetting(key="registration_open", value="true"),
                    RegistrationQueue(
                        telegram_id=12345,
                        abs_username="queued",
                        status=RegistrationQueueStatus.DONE,
                        position=1,
                        result_password="restored-secret",
                        notification_delivered=False,
                    ),
                ]
            )

    from absbot.backup import dump_database, restore_database

    backup_file = await dump_database(engine, str(tmp_path / "backups"))
    assert backup_file.exists()

    # 清空再恢复
    async with factory() as sess:
        async with sess.begin():
            await sess.execute(delete(RegistrationQueue))
            await sess.execute(delete(BotSetting))

    await restore_database(engine, backup_file)

    async with factory() as sess:
        row = (await sess.execute(select(BotSetting).where(BotSetting.key == "registration_open"))).scalar_one()
        queue_item = (
            await sess.execute(
                select(RegistrationQueue).where(RegistrationQueue.telegram_id == 12345)
            )
        ).scalar_one()
        assert row.value == "true"
        assert queue_item.abs_username == "queued"
        assert queue_item.result_password == "restored-secret"
