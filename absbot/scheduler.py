from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import FSInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncEngine

from absbot.backup import cleanup_old_backups, dump_database, list_local_backups
from absbot.config import Settings
from absbot.keyboards import registration_announcement_keyboard
from absbot.leaderboard import LeaderboardService, _chinese_date, format_leaderboard_message
from absbot.panels import DEFAULT_PANEL_PHOTO_PATH
from absbot.service import (
    ActivityCheckResult,
    ActivityUserResult,
    ExpirationProcessResult,
    ExpirationUserResult,
    MembershipService,
    RegistrationQueueProcessResult,
    SystemSettings,
)
from absbot.timeutils import format_dt

logger = logging.getLogger(__name__)


def create_scheduler(
    service: MembershipService,
    bot: Bot,
    engine: AsyncEngine,
    settings: Settings,
    *,
    timezone: str,
    leaderboard_service: LeaderboardService,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        run_leaderboard_sync_job,
        CronTrigger(hour=2, minute=0, timezone=timezone),
        args=[leaderboard_service, service, bot],
        id="daily-leaderboard-sync",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_active_renewal_job,
        CronTrigger(hour=3, minute=0, timezone=timezone),
        args=[service, bot],
        id="daily-active-renewal",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_points_renewal_job,
        CronTrigger(hour=4, minute=0, timezone=timezone),
        args=[service, bot],
        id="daily-points-renewal",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_expiration_enforcement_job,
        CronTrigger(hour=5, minute=0, timezone=timezone),
        args=[service, bot],
        id="daily-expiration-enforcement",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_backup_job,
        CronTrigger(hour=6, minute=0, timezone=timezone),
        args=[engine, bot, settings],
        id="daily-backup",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_daily_leaderboard_job,
        CronTrigger(hour=20, minute=0, timezone=timezone),
        args=[leaderboard_service, service, bot, timezone],
        id="daily-leaderboard-push",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_weekly_leaderboard_job,
        CronTrigger(day_of_week="mon", hour=20, minute=0, timezone=timezone),
        args=[leaderboard_service, service, bot, timezone],
        id="weekly-leaderboard-push",
        replace_existing=True,
        max_instances=1,
    )
    return scheduler


async def run_active_renewal_job(
    service: MembershipService, bot: Bot, *, force: bool = False
) -> ActivityCheckResult:
    public = await service.get_public_settings()
    if not force and not public.active_retention_enabled:
        logger.info("活跃续期未开启，跳过每日活跃续期任务")
        return ActivityCheckResult(total_synced=0, disabled=[], deleted=[])
    result = await service.process_active_renewals(force=force)
    system = await service.get_system_settings()
    await notify_active_renewal_result(bot, system, result)
    return result


async def run_leaderboard_sync_job(
    leaderboard_service: LeaderboardService,
    service: MembershipService,
    bot: Bot,
) -> None:
    """02:00 daily — pull new sessions from ABS and refresh TG display names."""
    count = await leaderboard_service.sync_sessions()
    logger.info("收听记录同步完成，新增 %d 条", count)
    system = await service.get_system_settings()
    if system.main_group_chat_id is not None:
        name_count = await leaderboard_service.sync_display_names(bot, system.main_group_chat_id)
        logger.info("用户显示名称更新 %d 位", name_count)


async def run_daily_leaderboard_job(
    leaderboard_service: LeaderboardService,
    service: MembershipService,
    bot: Bot,
    tz_name: str,
    *,
    force: bool = False,
) -> None:
    """20:00 daily — push previous-24h leaderboard to main group."""
    public = await service.get_public_settings()
    if not force and not public.daily_leaderboard_enabled:
        logger.info("每日榜推送未开启，跳过定时任务")
        return
    system = await service.get_system_settings()
    if system.main_group_chat_id is None:
        return
    now = datetime.now(tz=timezone.utc)
    end = now
    start = end - timedelta(days=1)
    local_end = end.astimezone(ZoneInfo(tz_name))
    period_label = _chinese_date(local_end)
    result = await leaderboard_service.get_leaderboard(start, end)
    text = format_leaderboard_message("daily", period_label, result)
    await safe_send_message(bot, system.main_group_chat_id, text)


async def run_weekly_leaderboard_job(
    leaderboard_service: LeaderboardService,
    service: MembershipService,
    bot: Bot,
    tz_name: str,
    *,
    force: bool = False,
) -> None:
    """Monday 20:00 — push previous-7-day leaderboard to main group."""
    public = await service.get_public_settings()
    if not force and not public.weekly_leaderboard_enabled:
        logger.info("每周榜推送未开启，跳过定时任务")
        return
    system = await service.get_system_settings()
    if system.main_group_chat_id is None:
        return
    now = datetime.now(tz=timezone.utc)
    end = now
    start = end - timedelta(days=7)
    local_start = start.astimezone(ZoneInfo(tz_name))
    local_end = end.astimezone(ZoneInfo(tz_name))
    period_label = f"{_chinese_date(local_start)}－{_chinese_date(local_end)}"
    result = await leaderboard_service.get_leaderboard(start, end)
    text = format_leaderboard_message("weekly", period_label, result)
    await safe_send_message(bot, system.main_group_chat_id, text)



async def run_points_renewal_job(
    service: MembershipService, bot: Bot, *, force: bool = False
) -> ExpirationProcessResult:
    public = await service.get_public_settings()
    if not force and not public.points_renewal_enabled:
        logger.info("积分续期未开启，跳过积分续期任务")
        return ExpirationProcessResult(
            active_renewed=[], points_renewed=[], disabled=[], deleted=[]
        )
    result = await service.process_points_renewals(force=force)
    system = await service.get_system_settings()
    await notify_points_renewal_result(bot, system, result)
    return result


async def run_expiration_enforcement_job(
    service: MembershipService, bot: Bot, *, force: bool = False
) -> ExpirationProcessResult:
    public = await service.get_public_settings()
    if not force and not public.expiration_enforcement_enabled:
        logger.info("到期检查未开启，跳过到期检查任务")
        return ExpirationProcessResult(
            active_renewed=[], points_renewed=[], disabled=[], deleted=[]
        )
    result = await service.process_expiration_enforcement(force=force)
    system = await service.get_system_settings()
    await notify_expiration_enforcement_result(bot, system, result)
    return result


async def run_registration_queue_worker(
    service: MembershipService,
    bot: Bot,
    *,
    empty_sleep_seconds: float = 3.0,
) -> None:
    while True:
        task = asyncio.create_task(process_registration_queue_once(service, bot))
        try:
            processed = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        except Exception:
            logger.exception("注册队列处理循环失败")
            await asyncio.sleep(empty_sleep_seconds)
            continue
        if not processed:
            await asyncio.sleep(empty_sleep_seconds)
            continue
        await asyncio.sleep(0)


async def process_registration_queue_once(service: MembershipService, bot: Bot) -> bool:
    result = await service.process_next_registration()
    processed_new_registration = result is not None
    if result is None:
        result = await service.get_next_registration_notification()
    if result is None:
        return False
    public = await service.get_public_settings()
    delivered = await notify_registration_result(bot, public.server_lines, result)
    if delivered:
        await service.mark_registration_queue_notified(result.queue_id)
    else:
        logger.warning("向用户 %s 发送注册结果通知失败", result.telegram_id)
    if result.registration_closed:
        await sync_registration_announcement(bot, service)
    return processed_new_registration or delivered


async def notify_registration_result(
    bot: Bot,
    server_lines: str,
    result: RegistrationQueueProcessResult,
) -> bool:
    if result.success:
        text = (
            "✅ 账号创建成功\n\n"
            f"用户名：<code>{html.escape(result.username or '')}</code>\n"
            f"初始密码：<code>{html.escape(result.initial_password or '')}</code>\n"
            f"有效期至：{format_dt(result.expires_at)}\n\n"
            f"线路信息：\n{html.escape(server_lines)}"
        )
    else:
        text = f"❌ 注册失败：{html.escape(result.error_message or '未知错误')}\n"
    return await safe_send_message(bot, result.telegram_id, text)


def _resolve_announcement_photo(
    configured_path: str | None,
    *,
    use_default: bool,
) -> str | FSInputFile | None:
    """Return a photo input for the announcement, or None if no photo should be used."""
    candidates: list[str | Path] = []
    if configured_path:
        path_str = configured_path.strip()
        if path_str:
            parsed = urlparse(path_str)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                candidates.append(path_str)
            else:
                candidates.append(Path(path_str))
    if use_default:
        candidates.append(DEFAULT_PANEL_PHOTO_PATH)
    for candidate in candidates:
        if isinstance(candidate, str):
            return candidate
        if candidate.is_file():
            return FSInputFile(candidate)
        logger.warning("公告图片文件不可用：%s", candidate)
    return None


async def sync_registration_announcement(
    bot,
    service: MembershipService,
    *,
    alert_target=None,
) -> None:
    if bot is None:
        return
    system = await service.get_system_settings()
    if system.main_group_chat_id is None:
        return
    public = await service.get_public_settings()
    text = registration_announcement_text(public)
    reply_markup = None
    is_open = public.registration_open and public.registration_slots > 0
    if is_open:
        me = await bot.get_me()
        username = (getattr(me, "username", "") or "").lstrip("@")
        if username:
            reply_markup = registration_announcement_keyboard(username)
    photo_input = _resolve_announcement_photo(system.panel_photo_path, use_default=True)
    chat_id, message_id = await service.get_registration_announcement_message()
    if chat_id is not None and message_id is not None:
        try:
            if photo_input is not None:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=text,
                    reply_markup=reply_markup,
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            return
        except TelegramAPIError:
            pass
    try:
        if photo_input is not None:
            sent = await bot.send_photo(
                system.main_group_chat_id,
                photo_input,
                caption=text,
                reply_markup=reply_markup,
            )
        else:
            sent = await bot.send_message(
                system.main_group_chat_id,
                text,
                reply_markup=reply_markup,
            )
    except TelegramAPIError as exc:
        if alert_target is not None:
            await alert_target.answer(f"同步群组注册公告失败：{html.escape(str(exc))}")
        return
    await service.set_registration_announcement_message(
        chat_id=sent.chat.id,
        message_id=sent.message_id,
    )


def registration_announcement_text(settings) -> str:
    if settings.registration_open and settings.registration_slots > 0:
        return (
            "📝 开放注册中\n\n"
            f"剩余名额：{settings.registration_slots}\n"
            "点击下方按钮私聊机器人以创建您的 Audiobookshelf 账号。"
        )
    return "📝 注册已关闭\n\n当前名额已满，请等待下次开放。"


async def notify_points_renewal_result(
    bot: Bot,
    system: SystemSettings,
    result: ExpirationProcessResult,
) -> None:
    if not result.has_changes:
        return

    if system.main_group_chat_id is not None:
        await safe_send_message(
            bot,
            system.main_group_chat_id,
            _points_renewal_group_summary(result),
        )

    for user in result.points_renewed:
        await safe_send_message(bot, user.telegram_id, _points_renewed_user_text(user))


async def notify_expiration_enforcement_result(
    bot: Bot,
    system: SystemSettings,
    result: ExpirationProcessResult,
) -> None:
    if not result.has_changes:
        return

    if system.main_group_chat_id is not None:
        await safe_send_message(
            bot,
            system.main_group_chat_id,
            _expiration_group_summary(result),
        )

    for user in result.deleted:
        await safe_send_message(bot, user.telegram_id, _deleted_user_text(user))

    for user in result.disabled:
        await safe_send_message(bot, user.telegram_id, _disabled_user_text(user))


async def notify_active_renewal_result(
    bot: Bot,
    system: SystemSettings,
    result: ActivityCheckResult,
) -> None:
    if system.main_group_chat_id is not None:
        await safe_send_message(
            bot,
            system.main_group_chat_id,
            _active_renewal_group_summary(result),
        )

    for user in result.active_renewed:
        await safe_send_message(bot, user.telegram_id, _active_renewed_user_text(user))


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    max_attempts: int = 3,
    max_retry_after: int = 60,
) -> bool:
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            await bot.send_message(chat_id, text)
            return True
        except TelegramRetryAfter as exc:
            if exc.retry_after > max_retry_after:
                logger.warning(
                    "Telegram retry_after %s 超过上限 %s，跳过会话 %s",
                    exc.retry_after,
                    max_retry_after,
                    chat_id,
                )
                return False
            if attempt >= attempts:
                logger.warning(
                    "Telegram 频率限制在会话 %s 经过 %s 次重试后仍未解除",
                    chat_id,
                    attempts,
                )
                return False
            await asyncio.sleep(exc.retry_after)
        except TelegramAPIError:
            logger.exception("向会话 %s 发送 Telegram 消息失败", chat_id)
            return False
    return False


def _expiration_group_summary(result: ExpirationProcessResult) -> str:
    lines = [
        "<b>到期检查完成</b>",
        f"已禁用：{len(result.disabled)}",
        f"已删除：{len(result.deleted)}",
    ]
    if result.disabled:
        disabled = "、".join(_user_label(user) for user in result.disabled)
        lines.append(f"禁用用户：{disabled}")
    if result.deleted:
        deleted = "、".join(_user_label(user) for user in result.deleted)
        lines.append(f"删除用户：{deleted}")
    return "\n".join(lines)


def _points_renewal_group_summary(result: ExpirationProcessResult) -> str:
    lines = [
        "<b>积分续期完成</b>",
        f"积分续期：{len(result.points_renewed)}",
    ]
    if result.points_renewed:
        renewed = "、".join(_user_label(user) for user in result.points_renewed)
        lines.append(f"续期用户：{renewed}")
    return "\n".join(lines)


def _disabled_user_text(user: ExpirationUserResult) -> str:
    account = _user_label(user)
    lines = [
        "<b>您的账号已到期</b>",
        f"您的 Audiobookshelf 账号 {account} 因到期已停用。",
        "请联系管理员，或通过积分续期以重新激活。",
    ]
    return "\n".join(lines)


def _points_renewed_user_text(user: ExpirationUserResult) -> str:
    account = _user_label(user)
    lines = [
        "<b>积分续期成功</b>",
        f"你的 Audiobookshelf 账号 {account} 已通过积分自动续期，消耗 {user.points_spent} 积分。",
        f"有效期延长至：{format_dt(user.expires_at)}",
    ]
    return "\n".join(lines)


def _deleted_user_text(user: ExpirationUserResult) -> str:
    account = _user_label(user)
    lines = [
        "<b>账号已删除</b>",
        f"你的 Audiobookshelf 账号 {account} 因禁用后超过保留天数，已被系统删除。",
    ]
    return "\n".join(lines)


def _user_label(user: ExpirationUserResult) -> str:
    if user.abs_username:
        return html.escape(user.abs_username)
    return html.escape(user.abs_user_id)


def _active_renewal_group_summary(result: ActivityCheckResult) -> str:
    lines = [
        "<b>活跃续期完成</b>",
        f"共检测 {result.total_synced} 位用户",
        f"活跃续期：{len(result.active_renewed)}",
    ]
    if result.active_renewed:
        names = "、".join(
            html.escape(u.abs_username or u.abs_user_id) for u in result.active_renewed
        )
        lines.append(f"续期用户：{names}")
    return "\n".join(lines)


def _active_renewed_user_text(user: ActivityUserResult) -> str:
    account = html.escape(user.abs_username or user.abs_user_id)
    lines = [
        "<b>活跃续期成功</b>",
        f"你的 Audiobookshelf 账号 {account} 已检测到活跃并自动续期成功。",
    ]
    if user.expires_at:
        lines.append(f"有效期已延长至：{format_dt(user.expires_at)}")
    return "\n".join(lines)


def _activity_disabled_user_text(user: ActivityUserResult) -> str:
    account = html.escape(user.abs_username or user.abs_user_id)
    lines = [
        "<b>账号因长期未活跃停用</b>",
        f"您的 Audiobookshelf 账号 {account} 因超过活跃窗口期已被系统停用。",
        "请联系管理员恢复访问。",
    ]
    return "\n".join(lines)


async def run_backup_job(engine: AsyncEngine, bot: Bot, settings: Settings) -> None:
    """每日定时备份任务：dump → 发送给 owner → 清理旧备份。"""
    try:
        backup_path = await dump_database(engine, settings.backup_dir)
    except Exception as exc:
        logger.error("数据库备份失败：%s", exc)
        if settings.owner_tg_id:
            await safe_send_message(bot, settings.owner_tg_id, f"❌ 数据库备份失败：{exc}")
        return

    if settings.owner_tg_id:
        try:
            await bot.send_document(
                settings.owner_tg_id,
                FSInputFile(backup_path),
                caption=f"🗄 数据库备份\n{backup_path.name}",
            )
        except Exception as exc:
            logger.error("备份文件发送失败：%s", exc)
            await safe_send_message(
                bot,
                settings.owner_tg_id,
                f"⚠️ 备份已生成（{backup_path.name}），但发送失败：{exc}",
            )

    deleted = cleanup_old_backups(settings.backup_dir, settings.backup_keep_count)
    logger.info("备份清理完成，删除 %d 份旧备份", deleted)
    remaining = len(list_local_backups(settings.backup_dir))
    if settings.owner_tg_id:
        await safe_send_message(
            bot,
            settings.owner_tg_id,
            f"✅ 备份完成：{backup_path.name}，共保留 {remaining} 份",
        )
