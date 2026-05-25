from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from absbot.leaderboard import (
    LeaderboardEntry,
    LeaderboardResult,
    LeaderboardService,
    _chinese_date,
    format_leaderboard_message,
)
from absbot.models import ListeningSession, TgUser

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session(
    abs_session_id: str,
    abs_user_id: str,
    played_at: datetime,
    display_title: str = "Book A",
    library_item_id: str = "lib1",
) -> dict:
    """Build a fake ABS session dict."""
    return {
        "id": abs_session_id,
        "userId": abs_user_id,
        "libraryItemId": library_item_id,
        "displayTitle": display_title,
        "startedAt": int(played_at.timestamp() * 1000),
    }


def _make_service(session_factory, abs_client) -> LeaderboardService:
    return LeaderboardService(session_factory, abs_client)


# ---------------------------------------------------------------------------
# sync_sessions: basic insert
# ---------------------------------------------------------------------------


async def test_sync_sessions_inserts_new(session_factory, abs_client):
    now = datetime.now(tz=UTC)
    abs_client.users = {"u1": {"id": "u1"}}
    abs_client.sessions_db["u1"] = [_session("s1", "u1", now)]

    svc = _make_service(session_factory, abs_client)
    # Set last_sync_at to before the session
    await svc.set_last_sync_at(now - timedelta(hours=1))
    count = await svc.sync_sessions()

    assert count == 1
    async with session_factory() as session:
        row = await session.scalar(
            select(ListeningSession).where(ListeningSession.abs_session_id == "s1")
        )
    assert row is not None
    assert row.abs_user_id == "u1"
    assert row.display_title == "Book A"


async def test_sync_sessions_skips_old(session_factory, abs_client):
    now = datetime.now(tz=UTC)
    abs_client.users = {"u1": {"id": "u1"}}
    # Session is 2 hours old, last_sync_at is 1 hour ago
    abs_client.sessions_db["u1"] = [_session("s_old", "u1", now - timedelta(hours=2))]

    svc = _make_service(session_factory, abs_client)
    await svc.set_last_sync_at(now - timedelta(hours=1))
    count = await svc.sync_sessions()

    assert count == 0


async def test_sync_sessions_deduplicates(session_factory, abs_client):
    now = datetime.now(tz=UTC)
    abs_client.users = {"u1": {"id": "u1"}}
    abs_client.sessions_db["u1"] = [_session("s_dup", "u1", now)]

    svc = _make_service(session_factory, abs_client)
    await svc.set_last_sync_at(now - timedelta(hours=1))
    first = await svc.sync_sessions()

    # Reset last_sync_at to before the session again so it's "new" on next sync
    await svc.set_last_sync_at(now - timedelta(hours=1))
    second = await svc.sync_sessions()

    assert first == 1
    assert second == 0  # duplicate skipped


async def test_sync_sessions_stops_on_empty_page(session_factory, abs_client):
    now = datetime.now(tz=UTC)
    abs_client.users = {"u1": {"id": "u1"}}
    # Only page 0 has data; page 1 returns empty
    abs_client.sessions_db["u1"] = [_session("s2", "u1", now)]

    svc = _make_service(session_factory, abs_client)
    await svc.set_last_sync_at(now - timedelta(hours=1))
    count = await svc.sync_sessions()

    assert count == 1


async def test_sync_sessions_stops_when_oldest_is_old(session_factory, abs_client):
    now = datetime.now(tz=UTC)
    abs_client.users = {"u1": {"id": "u1"}}
    # Two sessions: newest is new, oldest is old (triggers pagination stop)
    abs_client.sessions_db["u1"] = [
        _session("s_new", "u1", now, display_title="New"),
        _session("s_old", "u1", now - timedelta(hours=2), display_title="Old"),
    ]

    svc = _make_service(session_factory, abs_client)
    await svc.set_last_sync_at(now - timedelta(hours=1))
    count = await svc.sync_sessions()

    # Only the new session is inserted; the old one is filtered and pagination stops
    assert count == 1
    async with session_factory() as session:
        row = await session.scalar(
            select(ListeningSession).where(ListeningSession.abs_session_id == "s_new")
        )
    assert row is not None


# ---------------------------------------------------------------------------
# cleanup_old_sessions
# ---------------------------------------------------------------------------


async def test_cleanup_old_sessions(session_factory, abs_client):
    svc = _make_service(session_factory, abs_client)

    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                ListeningSession(
                    abs_session_id="old_s",
                    abs_user_id="u1",
                    library_item_id="lib1",
                    display_title="Old Book",
                    played_at=now - timedelta(days=100),
                )
            )
            session.add(
                ListeningSession(
                    abs_session_id="new_s",
                    abs_user_id="u1",
                    library_item_id="lib1",
                    display_title="New Book",
                    played_at=now - timedelta(days=1),
                )
            )

    deleted = await svc.cleanup_old_sessions(keep_days=90)
    assert deleted == 1

    async with session_factory() as session:
        remaining = await session.scalar(
            select(ListeningSession).where(ListeningSession.abs_session_id == "new_s")
        )
    assert remaining is not None


# ---------------------------------------------------------------------------
# get_leaderboard: book chart
# ---------------------------------------------------------------------------


async def test_get_leaderboard_books_ranking(session_factory, abs_client):
    svc = _make_service(session_factory, abs_client)
    now = datetime.now(tz=UTC)
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    async with session_factory() as session:
        async with session.begin():
            # lib2 has 3 sessions, lib1 has 1 — lib2 should rank first
            for i in range(3):
                session.add(
                    ListeningSession(
                        abs_session_id=f"b2s{i}",
                        abs_user_id="u1",
                        library_item_id="lib2",
                        display_title="Book B",
                        played_at=now,
                    )
                )
            session.add(
                ListeningSession(
                    abs_session_id="b1s0",
                    abs_user_id="u1",
                    library_item_id="lib1",
                    display_title="Book A",
                    played_at=now,
                )
            )

    result = await svc.get_leaderboard(start, end)
    assert result.book_entries[0].label == "Book B"
    assert result.book_entries[0].count == 3
    assert result.book_entries[1].label == "Book A"
    assert result.book_entries[1].count == 1
    assert result.total_sessions == 4


# ---------------------------------------------------------------------------
# get_leaderboard: user chart display name
# ---------------------------------------------------------------------------


async def test_get_leaderboard_users_display_name(session_factory, abs_client):
    svc = _make_service(session_factory, abs_client)
    now = datetime.now(tz=UTC)
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    async with session_factory() as session:
        async with session.begin():
            session.add(TgUser(telegram_id=12345, abs_user_id="u1", tg_display_name="Alice"))
            session.add(
                ListeningSession(
                    abs_session_id="us1",
                    abs_user_id="u1",
                    library_item_id="lib1",
                    display_title="Book",
                    played_at=now,
                )
            )

    result = await svc.get_leaderboard(start, end)
    assert result.user_entries[0].label == "Alice"
    assert result.user_entries[0].count == 1


async def test_get_leaderboard_users_fallback_to_telegram_id(session_factory, abs_client):
    svc = _make_service(session_factory, abs_client)
    now = datetime.now(tz=UTC)
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    async with session_factory() as session:
        async with session.begin():
            # User with no tg_display_name
            session.add(TgUser(telegram_id=99887766, abs_user_id="u2"))
            session.add(
                ListeningSession(
                    abs_session_id="uf1",
                    abs_user_id="u2",
                    library_item_id="lib1",
                    display_title="Book",
                    played_at=now,
                )
            )

    result = await svc.get_leaderboard(start, end)
    assert result.user_entries[0].label == "用户#7766"


async def test_get_leaderboard_users_fallback_abs_user_id(session_factory, abs_client):
    """User not in tg_users at all — falls back to abs_user_id."""
    svc = _make_service(session_factory, abs_client)
    now = datetime.now(tz=UTC)
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    async with session_factory() as session:
        async with session.begin():
            session.add(
                ListeningSession(
                    abs_session_id="ua1",
                    abs_user_id="orphan_user",
                    library_item_id="lib1",
                    display_title="Book",
                    played_at=now,
                )
            )

    result = await svc.get_leaderboard(start, end)
    assert result.user_entries[0].label == "orphan_user"


async def test_get_leaderboard_empty(session_factory, abs_client):
    svc = _make_service(session_factory, abs_client)
    now = datetime.now(tz=UTC)
    result = await svc.get_leaderboard(now - timedelta(hours=1), now + timedelta(hours=1))
    assert result.book_entries == []
    assert result.user_entries == []
    assert result.total_sessions == 0


# ---------------------------------------------------------------------------
# format_leaderboard_message
# ---------------------------------------------------------------------------


def test_format_leaderboard_message_daily():
    result = LeaderboardResult(
        book_entries=[
            LeaderboardEntry(label="Book Gold", count=5),
            LeaderboardEntry(label="Book Silver", count=3),
            LeaderboardEntry(label="Book Bronze", count=2),
            LeaderboardEntry(label="Book 4", count=1),
        ],
        user_entries=[LeaderboardEntry(label="Alice", count=7)],
        total_sessions=18,
    )
    text = format_leaderboard_message("daily", "5月24日", result)
    assert "每日收听榜" in text
    assert "5月24日" in text
    assert "🥇" in text
    assert "Book Gold" in text
    assert "5次" in text
    assert "Alice" in text
    assert "共 18 次收听会话" in text


def test_format_leaderboard_message_weekly():
    result = LeaderboardResult(book_entries=[], user_entries=[], total_sessions=0)
    text = format_leaderboard_message("weekly", "5月18日－5月24日", result)
    assert "每周收听榜" in text
    assert "暂无数据" in text


def test_format_leaderboard_message_truncates_long_title():
    long_title = "A" * 40
    result = LeaderboardResult(
        book_entries=[LeaderboardEntry(label=long_title, count=1)],
        user_entries=[],
        total_sessions=1,
    )
    text = format_leaderboard_message("daily", "5月24日", result)
    assert "…" in text


# ---------------------------------------------------------------------------
# _chinese_date helper
# ---------------------------------------------------------------------------


def test_chinese_date():
    dt = datetime(2026, 5, 3, 10, 0, tzinfo=UTC)
    assert _chinese_date(dt) == "5月3日"
