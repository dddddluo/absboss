from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from absbot.models import BotSetting, ListeningSession, TgUser
from absbot.timeutils import from_millis

logger = logging.getLogger(__name__)

_LAST_SYNC_KEY = "leaderboard_last_sync_at"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LeaderboardEntry:
    label: str
    count: int


@dataclass
class LeaderboardResult:
    book_entries: list[LeaderboardEntry]
    user_entries: list[LeaderboardEntry]
    total_sessions: int


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LeaderboardService:
    def __init__(self, session_factory: async_sessionmaker, abs_client) -> None:
        self.session_factory = session_factory
        self.abs_client = abs_client

    # ------------------------------------------------------------------ sync

    async def sync_sessions(self) -> int:
        """Pull new listening sessions from ABS for all users. Returns count inserted."""
        last_sync_at = await self.get_last_sync_at()
        since_millis = int(last_sync_at.timestamp() * 1000)
        sync_start = datetime.now(tz=timezone.utc)

        users_data = await self.abs_client.list_users()
        total_inserted = 0
        for user_data in users_data:
            abs_user_id = user_data.get("id") or ""
            if not abs_user_id:
                continue
            new_sessions = await self._fetch_new_sessions(abs_user_id, since_millis)
            inserted = await self._upsert_sessions(abs_user_id, new_sessions)
            total_inserted += inserted

        await self.set_last_sync_at(sync_start)
        await self.cleanup_old_sessions()
        return total_inserted

    async def _fetch_new_sessions(self, abs_user_id: str, since_millis: int) -> list[dict]:
        """Paginate ABS sessions for one user, collecting those newer than since_millis."""
        new_sessions: list[dict] = []
        for page in range(10):
            try:
                data = await self.abs_client.get_listening_sessions_page(
                    abs_user_id, page=page, items_per_page=50
                )
            except Exception:
                logger.warning("拉取用户 %s 收听记录失败（page=%d）", abs_user_id, page)
                break
            sessions = data.get("sessions") or []
            if not sessions:
                break
            for s in sessions:
                if (s.get("startedAt") or 0) > since_millis:
                    new_sessions.append(s)
            # ABS returns newest-first; stop once the oldest item on this page is old enough
            if (sessions[-1].get("startedAt") or 0) <= since_millis:
                break
        return new_sessions

    async def _upsert_sessions(self, abs_user_id: str, sessions: list[dict]) -> int:
        if not sessions:
            return 0
        inserted = 0
        async with self.session_factory() as session:
            async with session.begin():
                for s in sessions:
                    sid = s.get("id") or ""
                    if not sid:
                        continue
                    existing = await session.scalar(
                        select(ListeningSession).where(ListeningSession.abs_session_id == sid)
                    )
                    if existing is not None:
                        continue
                    started_ms = s.get("startedAt") or 0
                    played_at = (
                        from_millis(started_ms)
                        if started_ms
                        else datetime.now(tz=timezone.utc)
                    )
                    session.add(
                        ListeningSession(
                            abs_session_id=sid,
                            abs_user_id=abs_user_id,
                            library_item_id=s.get("libraryItemId") or "",
                            display_title=s.get("displayTitle") or "",
                            played_at=played_at,
                        )
                    )
                    inserted += 1
        return inserted

    async def sync_display_names(self, bot, main_group_chat_id: int) -> int:
        """Refresh tg_display_name for all users via get_chat_member. Returns count updated."""
        async with self.session_factory() as session:
            rows = await session.execute(
                select(TgUser.telegram_id).where(TgUser.abs_user_id.isnot(None))
            )
            telegram_ids = [row[0] for row in rows]

        updated = 0
        semaphore = asyncio.Semaphore(5)

        async def refresh_one(telegram_id: int) -> None:
            nonlocal updated
            async with semaphore:
                try:
                    member = await bot.get_chat_member(main_group_chat_id, telegram_id)
                    if hasattr(member, "user") and member.user:
                        display_name = member.user.full_name
                        async with self.session_factory() as sess:
                            async with sess.begin():
                                await sess.execute(
                                    update(TgUser)
                                    .where(TgUser.telegram_id == telegram_id)
                                    .values(tg_display_name=display_name)
                                )
                        updated += 1
                except Exception:
                    logger.debug("更新用户 %s 显示名称失败", telegram_id)

        await asyncio.gather(*[refresh_one(tid) for tid in telegram_ids])
        return updated

    async def cleanup_old_sessions(self, keep_days: int = 90) -> int:
        """Delete sessions older than keep_days. Returns count deleted."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=keep_days)
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    delete(ListeningSession).where(ListeningSession.played_at < cutoff)
                )
        return result.rowcount

    # ------------------------------------------------------------------ query

    async def get_leaderboard(
        self,
        start: datetime,
        end: datetime,
        *,
        top_n: int = 10,
    ) -> LeaderboardResult:
        """Return top-N book and user leaderboards for the given time window."""
        async with self.session_factory() as session:
            # book chart
            book_rows = (
                await session.execute(
                    select(
                        ListeningSession.library_item_id,
                        ListeningSession.display_title,
                        func.count(ListeningSession.id).label("cnt"),
                    )
                    .where(ListeningSession.played_at >= start)
                    .where(ListeningSession.played_at < end)
                    .group_by(
                        ListeningSession.library_item_id,
                        ListeningSession.display_title,
                    )
                    .order_by(func.count(ListeningSession.id).desc())
                    .limit(top_n)
                )
            ).all()
            books = [
                LeaderboardEntry(
                    label=row.display_title or row.library_item_id,
                    count=row.cnt,
                )
                for row in book_rows
            ]

            # user chart
            user_rows = (
                await session.execute(
                    select(
                        ListeningSession.abs_user_id,
                        TgUser.telegram_id,
                        TgUser.tg_display_name,
                        func.count(ListeningSession.id).label("cnt"),
                    )
                    .outerjoin(TgUser, TgUser.abs_user_id == ListeningSession.abs_user_id)
                    .where(ListeningSession.played_at >= start)
                    .where(ListeningSession.played_at < end)
                    .group_by(
                        ListeningSession.abs_user_id,
                        TgUser.telegram_id,
                        TgUser.tg_display_name,
                    )
                    .order_by(func.count(ListeningSession.id).desc())
                    .limit(top_n)
                )
            ).all()
            users = []
            for row in user_rows:
                if row.tg_display_name:
                    label = row.tg_display_name
                elif row.telegram_id:
                    label = f"用户#{str(row.telegram_id)[-4:]}"
                else:
                    label = row.abs_user_id
                users.append(LeaderboardEntry(label=label, count=row.cnt))

            total = (
                await session.scalar(
                    select(func.count(ListeningSession.id))
                    .where(ListeningSession.played_at >= start)
                    .where(ListeningSession.played_at < end)
                )
            ) or 0

        return LeaderboardResult(
            book_entries=books,
            user_entries=users,
            total_sessions=total,
        )

    # ------------------------------------------------------------------ settings KV

    async def get_last_sync_at(self) -> datetime:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(BotSetting.value).where(BotSetting.key == _LAST_SYNC_KEY)
            )
        if row is None:
            return datetime.now(tz=timezone.utc) - timedelta(days=30)
        return datetime.fromisoformat(row)

    async def set_last_sync_at(self, value: datetime) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(BotSetting).where(BotSetting.key == _LAST_SYNC_KEY)
                )
                iso = value.isoformat()
                if existing is None:
                    session.add(BotSetting(key=_LAST_SYNC_KEY, value=iso))
                else:
                    existing.value = iso


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_leaderboard_message(kind: str, period_label: str, result: LeaderboardResult) -> str:
    """Render a leaderboard message in HTML parse mode."""
    title = "每日收听榜" if kind == "daily" else "每周收听榜"
    lines = [f"<b>📚 {title} · {html.escape(period_label)}</b>", ""]

    lines.append("🔖 最热书目 Top 10")
    if result.book_entries:
        medals: dict[int, str] = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, entry in enumerate(result.book_entries, 1):
            medal = medals.get(i, f"{i}.")
            label = entry.label if len(entry.label) <= 28 else entry.label[:27] + "…"
            lines.append(f"{medal} {html.escape(label)} · {entry.count}次")
    else:
        lines.append("暂无数据")
    lines.append("")

    lines.append("🎧 最活跃听众 Top 10")
    if result.user_entries:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, entry in enumerate(result.user_entries, 1):
            medal = medals.get(i, f"{i}.")
            lines.append(f"{medal} {html.escape(entry.label)} · {entry.count}次")
    else:
        lines.append("暂无数据")
    lines.append("")

    lines.append(f"📊 共 {result.total_sessions} 次收听会话")
    return "\n".join(lines)


def _chinese_date(dt: datetime) -> str:
    """Format datetime as Chinese date, e.g. '5月24日'."""
    return f"{dt.month}月{dt.day}日"
