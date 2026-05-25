from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ChatPermissions, Message, TelegramObject, Update

from absbot.abs_client import AudiobookshelfError
from absbot.config import Settings
from absbot.keyboards import user_panel_keyboard
from absbot.models import RebindRequestStatus, RedeemCodeType
from absbot.panels import replace_panel, send_panel
from absbot.service import MembershipService, PublicSettings, RebindRequestSnapshot, SystemSettings
from absbot.timeutils import format_dt

logger = logging.getLogger(__name__)

REBIND_REVIEW_SEND_TIMEOUT_SECONDS = 15


# ---------------------------------------------------------------------------
# Chat / command parsing helpers
# ---------------------------------------------------------------------------

def parse_pp_target(message: Message) -> int | None:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if not parts:
        return None
    command = parts[0].split("@", 1)[0].lower()
    if command != "/pp":
        return None
    if len(parts) == 2:
        try:
            return int(parts[1].strip())
        except ValueError:
            return None
    reply = getattr(message, "reply_to_message", None)
    from_user = getattr(reply, "from_user", None)
    if from_user is not None:
        return from_user.id
    return None


def is_private_start(message: Message) -> bool:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if not parts:
        return False
    command = parts[0].split("@", 1)[0].lower()
    chat_type = getattr(getattr(message, "chat", None), "type", None)
    return command == "/start" and chat_type == "private"


def _is_group_start_command_message(event: TelegramObject) -> bool:
    text = (getattr(event, "text", None) or "").strip()
    parts = text.split(maxsplit=1)
    if not parts:
        return False
    command = parts[0].split("@", 1)[0].lower()
    chat_type = getattr(getattr(event, "chat", None), "type", None)
    return command == "/start" and chat_type in {"group", "supergroup"}


def _start_payload(message: Message) -> str:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return ""
    return parts[1].strip().lower()


def _is_setup_message(message: Message) -> bool:
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        return False
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    return command == "/setup"


def _is_setup_callback(callback: CallbackQuery) -> bool:
    data = getattr(callback, "data", None) or ""
    return data == "admin:setup" or data.startswith("setup:")


def is_setup_update(update: Update) -> bool:
    if update.message is not None:
        return _is_setup_message(update.message)
    if update.callback_query is not None:
        return _is_setup_callback(update.callback_query)
    return False


def is_setup_event(event: TelegramObject, raw_state: str | None = None) -> bool:
    if raw_state and raw_state.startswith("SetupStates:"):
        return True
    if isinstance(event, Message):
        return _is_setup_message(event)
    if isinstance(event, CallbackQuery):
        return _is_setup_callback(event)
    return False


def is_allowed_chat_member_status(status, *, is_member: bool | None = None) -> bool:
    value = getattr(status, "value", status)
    if value in {"member", "administrator", "creator"}:
        return True
    if value == "restricted":
        return bool(is_member)
    return False


def _is_private_chat(message: Message | None) -> bool:
    return getattr(getattr(message, "chat", None), "type", None) == "private"


def parse_credentials(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("格式错误，请输入：用户名 密码")
    username = parts[0].strip()
    password = parts[1]
    if not username:
        raise ValueError("用户名不能为空")
    if not password:
        raise ValueError("密码不能为空")
    return username, password


def parse_code_payload(text: str, code_type: RedeemCodeType) -> tuple[str | None, int | None, int]:
    parts = text.split()
    if code_type == RedeemCodeType.WHITELIST and len(parts) in {1, 2, 3}:
        if len(parts) == 3:
            code = None if parts[0] == "-" else parts[0]
            return code, None, int(parts[2])
        if len(parts) == 2:
            code = None if parts[0] == "-" else parts[0]
            return code, None, int(parts[1])
        return None, None, int(parts[0])
    if len(parts) not in {2, 3}:
        raise ValueError("格式错误，请输入：days count")
    if len(parts) == 3:
        code = None if parts[0] == "-" else parts[0]
        return code, int(parts[1]), int(parts[2])
    return None, int(parts[0]), int(parts[1])


def format_created_codes_messages(codes: list[str], *, limit: int = 3900) -> list[str]:
    header = f"已创建 {len(codes)} 个兑换码："
    messages: list[str] = []
    current = header
    for code in codes:
        line = f"\n<code>{html.escape(code)}</code>"
        if len(current) + len(line) > limit:
            messages.append(current)
            current = header + line
        else:
            current += line
    messages.append(current)
    return messages


# ---------------------------------------------------------------------------
# Permission / auth helpers
# ---------------------------------------------------------------------------

def should_show_setup_notice(
    *,
    is_initialized: bool,
    is_owner: bool,
    is_admin: bool,
    owner_configured: bool,
) -> bool:
    _ = is_admin, owner_configured
    if is_initialized:
        return False
    return is_owner


def _can_run_setup(settings: Settings, telegram_id: int | None) -> bool:
    return settings.is_owner(telegram_id)


async def _require_admin(callback: CallbackQuery, settings: Settings) -> bool:
    if settings.is_admin(callback.from_user.id):
        return True
    await callback.answer("没有权限", show_alert=True)
    return False


def _is_admin_message(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and settings.is_admin(message.from_user.id)


# ---------------------------------------------------------------------------
# Message manipulation helpers
# ---------------------------------------------------------------------------

async def _handle_unauthorized_message(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError as exc:
        logger.warning("删除未授权消息失败：%s", exc)

    chat_type = getattr(getattr(message, "chat", None), "type", None)
    if chat_type not in {"group", "supergroup"}:
        return

    bot = getattr(message, "bot", None)
    from_user = getattr(message, "from_user", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if bot is None or from_user is None or chat_id is None:
        return

    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=from_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    except TelegramAPIError as exc:
        logger.warning(
            "在会话 %s 中禁言未授权用户 %s 失败：%s",
            chat_id,
            from_user.id,
            exc,
        )


async def _delete_group_command_message(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError as exc:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        logger.warning("删除群组指令消息失败，会话 %s：%s", chat_id, exc)


async def _edit_prompt_message(message: Message | None, text: str, *, reply_markup=None) -> None:
    if message is None:
        return
    if getattr(message, "photo", None):
        await message.edit_caption(caption=text, reply_markup=reply_markup)
        return
    await message.edit_text(text, reply_markup=reply_markup)


async def _try_delete_sensitive_message(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError as exc:
        logger.warning("删除敏感消息失败：%s", exc)


async def _delete_after(message: Message, delay: float) -> None:
    """等待 *delay* 秒后静默删除 *message*。"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except TelegramAPIError:
        pass


async def _answer_membership_required(event: TelegramObject, text: str) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer("请先加入主群组", show_alert=True)
        if event.message:
            await event.message.answer(text)
        return
    if isinstance(event, Message):
        await event.answer(text)


async def _notify_rebind_result(
    callback: CallbackQuery,
    request: RebindRequestSnapshot,
) -> None:
    status = "已同意" if request.status == RebindRequestStatus.APPROVED else "已拒绝"
    try:
        await callback.bot.send_message(
            request.requester_telegram_id,
            f"你的换绑申请{status}。\n账号：<code>{html.escape(request.abs_username)}</code>",
        )
    except TelegramAPIError:
        pass


# ---------------------------------------------------------------------------
# Panel dispatch helpers
# ---------------------------------------------------------------------------

class _BotChatPanelTarget:
    def __init__(self, bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id

    async def answer_photo(self, photo, *, caption, reply_markup=None, parse_mode=None):
        return await self.bot.send_photo(
            self.chat_id,
            photo,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    async def answer(self, text, *, reply_markup=None, parse_mode=None):
        return await self.bot.send_message(self.chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)


async def _panel_photo_path(service: MembershipService) -> str | None:
    return (await service.get_system_settings()).panel_photo_path


async def _optional_panel_photo_path(service: MembershipService) -> str | None:
    try:
        return await _panel_photo_path(service)
    except AttributeError:
        return None


async def _send_user_panel(
    message: Message,
    service: MembershipService,
    settings: Settings,
    telegram_id: int,
) -> None:
    public_settings = await service.get_public_settings()
    profile = await service.sync_profile_activity(telegram_id)
    await send_panel(
        message,
        _user_panel_text(profile),
        reply_markup=user_panel_keyboard(
            profile=profile,
            settings=public_settings,
            is_admin=settings.is_admin(telegram_id),
            include_close=not _is_private_chat(message),
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


async def _replace_user_panel(
    callback: CallbackQuery,
    service: MembershipService,
    settings: Settings,
    telegram_id: int,
) -> None:
    public_settings = await service.get_public_settings()
    profile = await service.sync_profile_activity(telegram_id)
    await replace_panel(
        callback,
        _user_panel_text(profile),
        reply_markup=user_panel_keyboard(
            profile=profile,
            settings=public_settings,
            is_admin=settings.is_admin(telegram_id),
            include_close=not _is_private_chat(callback.message),
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


async def _send_registration_username_prompt(message: Message, service: MembershipService) -> None:
    await send_panel(
        message,
        _registration_username_prompt(),
        panel_photo_path=await _optional_panel_photo_path(service),
    )


async def _send_registration_claim_notice(
    bot,
    target: int,
    *,
    reply_markup=None,
    panel_photo_path: str | None = None,
) -> None:
    await send_panel(
        _BotChatPanelTarget(bot, target),
        _registration_claim_text(),
        reply_markup=reply_markup,
        panel_photo_path=panel_photo_path,
    )


# ---------------------------------------------------------------------------
# Error message helpers
# ---------------------------------------------------------------------------

def _telegram_user_mention(telegram_id: int) -> str:
    return f'<a href="tg://user?id={telegram_id}">@{telegram_id}</a>'


def _abs_user_error_message(exc: AudiobookshelfError) -> str:
    text = str(exc)
    lower = text.lower()
    if "timeout" in lower or "超时" in text:
        return "连接服务器超时"
    if "request failed" in lower or "connect" in lower or "连接服务器失败" in text:
        return "连接服务器失败"
    if "username" in lower and ("taken" in lower or "already" in lower or "exists" in lower):
        return "用户名已存在"
    if "404" in lower or "not found" in lower or "用户不存在" in text:
        return "用户不存在"
    if lower.startswith("audiobookshelf api"):
        return "服务器请求失败，请稍后重试"
    return text


# ---------------------------------------------------------------------------
# Text builders
# ---------------------------------------------------------------------------

def _admin_panel_text(public: PublicSettings) -> str:
    return (
        "管理员面板\n"
        f"开放注册：{'开' if public.registration_open else '关'}，剩余 {public.registration_slots}\n"
        f"签到：{'开' if public.checkin_enabled else '关'}，"
        f"每次 {public.checkin_min_points}~{public.checkin_max_points} 分\n"
        f"活跃保号：{'开' if public.active_retention_enabled else '关'}，"
        f"{public.active_retention_window_days} 天内活跃续 {public.active_retention_extension_days} 天\n"
        f"积分续期：{'开' if public.points_renewal_enabled else '关'}，"
        f"{public.points_renewal_cost_points} 分续 {public.points_renewal_extension_days} 天\n"
        f"积分解禁：{'开' if public.points_unban_enabled else '关'}，"
        f"费用 {public.points_unban_cost_points} 积分"
    )


def _checkin_unban_panel_text(public: PublicSettings) -> str:
    return (
        "签到与解禁设置\n\n"
        f"🎁 签到：{'开' if public.checkin_enabled else '关'}，"
        f"每次 {public.checkin_min_points}~{public.checkin_max_points} 分\n"
        f"🔓 积分解禁：{'开' if public.points_unban_enabled else '关'}，"
        f"费用 {public.points_unban_cost_points} 积分"
    )


def _user_panel_text(profile) -> str:
    text = (
        "👤 个人面板\n"
        f"🆔 TG ID：<code>{profile.telegram_id}</code>\n"
        f"👤 ABS 账号：{html.escape(profile.abs_username or '未创建')}\n"
        f"🎟️ 注册资格：{profile.registration_credits}\n"
        f"⭐ 白名单：{'是' if profile.is_whitelisted else '否'}\n"
        f"💎 积分：{profile.points}"
    )
    if not profile.abs_user_id:
        return text
    return (
        f"{text}\n"
        f"⏳ 到期：{format_dt(profile.expires_at)}\n"
        f"🕒 最近登录：{format_dt(profile.last_seen_at)}\n"
        f"🎧 最近播放：{format_dt(profile.last_played_at)}"
    )


def _registration_username_prompt() -> str:
    return "请输入要创建的 Audiobookshelf 用户名。"


def _registration_claim_text() -> str:
    return "恭喜你获得了注册资格\n\n请点击下方按钮进入注册流程，创建 Audiobookshelf 账号。"


def _target_user_text(profile) -> str:
    text = (
        "👥 用户管理\n"
        f"🆔 TG ID：<code>{profile.telegram_id}</code>\n"
        f"👤 ABS 账号：{html.escape(profile.abs_username or '未创建')}\n"
        f"🎟️ 注册资格：{profile.registration_credits}\n"
        f"⭐ 白名单：{'已激活' if profile.is_whitelisted else '未激活'}\n"
        f"💎 积分：{profile.points}"
    )
    if not profile.abs_user_id:
        return f"{text}\n⚠️ 状态：{'已禁用' if profile.is_disabled else '正常'}"
    return (
        f"{text}\n"
        f"⏳ 到期：{format_dt(profile.expires_at)}\n"
        f"⚠️ 状态：{'已禁用' if profile.is_disabled else '正常'}"
    )


def _rebind_review_text(request: RebindRequestSnapshot) -> str:
    current = str(request.current_telegram_id) if request.current_telegram_id is not None else "无"
    reviewed = f"\n审核人：<code>{request.reviewed_by}</code>" if request.reviewed_by else ""
    reviewed_at = f"\n审核时间：{format_dt(request.reviewed_at)}" if request.reviewed_at else ""
    return (
        "换绑申请\n"
        f"状态：{_rebind_status_text(request.status)}\n"
        f"申请人 TG ID：<code>{request.requester_telegram_id}</code>\n"
        f"ABS 账号：<code>{html.escape(request.abs_username)}</code>\n"
        f"当前绑定 TG ID：<code>{current}</code>\n"
        f"申请时间：{format_dt(request.created_at)}"
        f"{reviewed}"
        f"{reviewed_at}"
    )


def _rebind_status_text(status: RebindRequestStatus) -> str:
    if status == RebindRequestStatus.APPROVED:
        return "已同意"
    if status == RebindRequestStatus.REJECTED:
        return "已拒绝"
    return "待审核"


def _build_tasks_panel_text(public: PublicSettings, scheduler: AsyncIOScheduler) -> str:
    def fmt_next(job_id: str) -> str:
        job = scheduler.get_job(job_id)
        if job and job.next_run_time:
            return job.next_run_time.strftime("%m/%d %H:%M")
        return "未知"

    return (
        "任务控制面板\n\n"
        f"🕒 活跃检测：{'开' if public.active_retention_enabled else '关'}"
        f"（每日 03:00，下次：{fmt_next('daily-activity-check')}）\n"
        f"   窗口 {public.active_retention_window_days} 天，禁用后留存 {public.active_retention_extension_days} 天\n\n"
        f"💎 积分续期：{'开' if public.points_renewal_enabled else '关'}"
        f"（每日 04:10，下次：{fmt_next('daily-expiration-check')}）"
    )


def _setup_summary_text(public: PublicSettings, system: SystemSettings) -> str:
    reg_status = f"开放（{public.registration_slots} 名额）" if public.registration_open else "关闭"
    checkin_status = (
        f"开启（{public.checkin_min_points}–{public.checkin_max_points} 分）"
        if public.checkin_enabled
        else "关闭"
    )
    active_status = (
        f"开启（活跃窗口 {public.active_retention_window_days} 天 / 续期 {public.active_retention_extension_days} 天）"
        if public.active_retention_enabled
        else "关闭"
    )
    renewal_status = (
        f"开启（{public.points_renewal_cost_points} 积分 / 续期 {public.points_renewal_extension_days} 天）"
        if public.points_renewal_enabled
        else "关闭"
    )
    disabled_delete_status = (
        f"禁用 {system.disabled_delete_after_days} 天后"
        if system.disabled_delete_after_days > 0
        else "关闭"
    )
    return (
        "✅ 初始化完成！当前设置摘要：\n\n"
        f"👥 主群组：{system.main_group_chat_id if system.main_group_chat_id is not None else '未设置'}\n"
        f"🔗 主群链接：{system.main_group_link or '未设置'}\n"
        f"📝 注册：{reg_status}\n"
        f"📅 默认注册天数：{system.default_register_days} 天\n"
        f"📡 线路：{public.server_lines[:40]}{'…' if len(public.server_lines) > 40 else ''}\n"
        f"🎁 签到：{checkin_status}\n"
        f"🕒 活跃保号：{active_status}\n"
        f"💎 积分续期：{renewal_status}\n"
        f"🖼️ 面板图片：{system.panel_photo_path or '默认'}\n"
        f"🔁 换绑审核群：{system.rebind_review_chat_id if system.rebind_review_chat_id is not None else '未启用'}\n"
        f"🧹 自动删除：{disabled_delete_status}"
    )
