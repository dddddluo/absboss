from __future__ import annotations

import html
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from absbot.abs_client import AudiobookshelfError
from absbot.config import Settings
from absbot.keyboards import (
    admin_panel_keyboard,
    checkin_unban_panel_keyboard,
    code_list_keyboard,
    code_panel_keyboard,
    registration_claim_keyboard,
    target_user_keyboard,
    tasks_panel_keyboard,
    users_page_keyboard,
)
from absbot.leaderboard import LeaderboardService
from absbot.panels import replace_panel, send_panel
from absbot.scheduler import (
    notify_activity_check_result,
    run_daily_leaderboard_job,
    run_expiration_job,
    run_weekly_leaderboard_job,
    sync_registration_announcement,
)
from absbot.service import MAX_REDEEM_CODES_PER_BATCH, MembershipService
from absbot.models import RedeemCodeType

from .helpers import (
    _abs_user_error_message,
    _admin_panel_text,
    _build_tasks_panel_text,
    _can_run_setup,
    _checkin_unban_panel_text,
    _edit_prompt_message,
    _handle_unauthorized_message,
    _is_admin_message,
    _is_private_chat,
    _notify_rebind_result,
    _panel_photo_path,
    _rebind_review_text,
    _replace_user_panel,
    _require_admin,
    _send_registration_claim_notice,
    _target_user_text,
    _telegram_user_mention,
    format_created_codes_messages,
    parse_code_payload,
)
from .states import AdminStates

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data == "admin:home")
async def admin_home(callback: CallbackQuery, service: MembershipService, settings: Settings) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer()
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _admin_panel_text(public),
        reply_markup=admin_panel_keyboard(
            include_start_back=_is_private_chat(callback.message),
            is_owner=_can_run_setup(settings, callback.from_user.id),
            owner_id=callback.from_user.id,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data == "admin:checkin_unban")
async def checkin_unban_panel(
    callback: CallbackQuery, service: MembershipService, settings: Settings
) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer()
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _checkin_unban_panel_text(public),
        reply_markup=checkin_unban_panel_keyboard(
            checkin_enabled=public.checkin_enabled,
            points_unban_enabled=public.points_unban_enabled,
            points_unban_cost_points=public.points_unban_cost_points,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data == "admin:start")
async def admin_start_panel(
    callback: CallbackQuery,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer()
    await _replace_user_panel(callback, service, settings, callback.from_user.id)


# ---------------------------------------------------------------------------
# Registration slots
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin:reg")
async def ask_registration_slots(
    callback: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer()
    await state.set_state(AdminStates.registration_slots)
    await _edit_prompt_message(callback.message, "请输入开放注册人数；输入 0 表示关闭注册。")


@router.message(AdminStates.registration_slots)
async def set_registration_slots(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not _is_admin_message(message, settings):
        await _handle_unauthorized_message(message)
        return
    try:
        slots = int((message.text or "").strip())
    except ValueError:
        await message.answer("请输入数字。")
        return
    await service.set_registration(opened=slots > 0, slots=slots)
    await sync_registration_announcement(message.bot, service, alert_target=message)
    await state.clear()
    public = await service.get_public_settings()
    await send_panel(
        message,
        _admin_panel_text(public),
        reply_markup=admin_panel_keyboard(
            include_start_back=_is_private_chat(message),
            is_owner=_can_run_setup(settings, message.from_user.id),
            owner_id=message.from_user.id,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


# ---------------------------------------------------------------------------
# Server lines
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin:lines")
async def ask_server_lines(
    callback: CallbackQuery, state: FSMContext, service: MembershipService, settings: Settings
) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer()
    await state.set_state(AdminStates.server_lines)
    public = await service.get_public_settings()
    prompt = "请发送新的服务器线路内容（支持 HTML 格式）。"
    if public.server_lines:
        prompt += f"\n\n当前内容（原始文本）：\n<pre>{html.escape(public.server_lines)}</pre>"
    await _edit_prompt_message(callback.message, prompt)


@router.message(AdminStates.server_lines)
async def set_server_lines(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not _is_admin_message(message, settings):
        await _handle_unauthorized_message(message)
        return
    text = message.text or ""
    await service.set_server_lines(text)
    await state.clear()
    public = await service.get_public_settings()
    await send_panel(
        message,
        _admin_panel_text(public),
        reply_markup=admin_panel_keyboard(
            include_start_back=_is_private_chat(message),
            is_owner=_can_run_setup(settings, message.from_user.id),
            owner_id=message.from_user.id,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


# ---------------------------------------------------------------------------
# Checkin & points unban
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin:checkin")
async def toggle_checkin(callback: CallbackQuery, service: MembershipService, settings: Settings) -> None:
    if not await _require_admin(callback, settings):
        return
    public = await service.get_public_settings()
    await service.set_checkin(
        enabled=not public.checkin_enabled,
        min_points=public.checkin_min_points,
        max_points=public.checkin_max_points,
    )
    await callback.answer("已切换签到开关")
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _checkin_unban_panel_text(public),
        reply_markup=checkin_unban_panel_keyboard(
            checkin_enabled=public.checkin_enabled,
            points_unban_enabled=public.points_unban_enabled,
            points_unban_cost_points=public.points_unban_cost_points,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data == "admin:checkinpoints")
async def ask_checkin_points(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _require_admin(callback, settings):
        return
    await state.set_state(AdminStates.checkin_points)
    await callback.answer()
    await _edit_prompt_message(callback.message, "请输入每次签到赠送的积分范围，格式：min max，例如：1 10。")


@router.message(AdminStates.checkin_points)
async def set_checkin_points(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not _is_admin_message(message, settings):
        await _handle_unauthorized_message(message)
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("格式错误，请输入：min max，例如：1 10。")
        return
    try:
        min_points, max_points = int(parts[0]), int(parts[1])
        public = await service.get_public_settings()
        await service.set_checkin(
            enabled=public.checkin_enabled,
            min_points=min_points,
            max_points=max_points,
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    public = await service.get_public_settings()
    await send_panel(
        message,
        _checkin_unban_panel_text(public),
        reply_markup=checkin_unban_panel_keyboard(
            checkin_enabled=public.checkin_enabled,
            points_unban_enabled=public.points_unban_enabled,
            points_unban_cost_points=public.points_unban_cost_points,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data == "admin:toggle_unban")
async def toggle_points_unban(
    callback: CallbackQuery, service: MembershipService, settings: Settings
) -> None:
    if not await _require_admin(callback, settings):
        return
    public = await service.get_public_settings()
    await service.set_points_unban(
        enabled=not public.points_unban_enabled,
        cost_points=public.points_unban_cost_points,
    )
    await callback.answer("已切换积分解禁开关")
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _checkin_unban_panel_text(public),
        reply_markup=checkin_unban_panel_keyboard(
            checkin_enabled=public.checkin_enabled,
            points_unban_enabled=public.points_unban_enabled,
            points_unban_cost_points=public.points_unban_cost_points,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data == "admin:set_unban_cost")
async def ask_unban_cost(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer()
    await state.set_state(AdminStates.unban_cost)
    await _edit_prompt_message(callback.message, "请输入积分解禁所需积分数（正整数）。")


@router.message(AdminStates.unban_cost)
async def set_unban_cost(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not _is_admin_message(message, settings):
        await _handle_unauthorized_message(message)
        return
    try:
        cost = int((message.text or "").strip())
        if cost <= 0:
            raise ValueError("积分数必须为正整数。")
        public = await service.get_public_settings()
        await service.set_points_unban(
            enabled=public.points_unban_enabled,
            cost_points=cost,
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    public = await service.get_public_settings()
    await send_panel(
        message,
        _checkin_unban_panel_text(public),
        reply_markup=checkin_unban_panel_keyboard(
            checkin_enabled=public.checkin_enabled,
            points_unban_enabled=public.points_unban_enabled,
            points_unban_cost_points=public.points_unban_cost_points,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


# ---------------------------------------------------------------------------
# Tasks panel
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin:tasks")
async def tasks_panel(
    callback: CallbackQuery,
    service: MembershipService,
    settings: Settings,
    scheduler: AsyncIOScheduler,
) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer()
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _build_tasks_panel_text(public, scheduler),
        reply_markup=tasks_panel_keyboard(
            active_enabled=public.active_retention_enabled,
            points_enabled=public.points_renewal_enabled,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data == "admin:active")
async def toggle_active_retention(
    callback: CallbackQuery, service: MembershipService, settings: Settings, scheduler: AsyncIOScheduler
) -> None:
    if not await _require_admin(callback, settings):
        return
    public = await service.get_public_settings()
    await service.set_active_retention(
        enabled=not public.active_retention_enabled,
        window_days=public.active_retention_window_days,
        extension_days=public.active_retention_extension_days,
    )
    await callback.answer("已切换活跃保号")
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _build_tasks_panel_text(public, scheduler),
        reply_markup=tasks_panel_keyboard(
            active_enabled=public.active_retention_enabled,
            points_enabled=public.points_renewal_enabled,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data == "admin:pointsrenew")
async def toggle_points_renewal(
    callback: CallbackQuery, service: MembershipService, settings: Settings, scheduler: AsyncIOScheduler
) -> None:
    if not await _require_admin(callback, settings):
        return
    public = await service.get_public_settings()
    await service.set_points_renewal(
        enabled=not public.points_renewal_enabled,
        cost_points=public.points_renewal_cost_points,
        extension_days=public.points_renewal_extension_days,
    )
    await callback.answer("已切换积分续期")
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _build_tasks_panel_text(public, scheduler),
        reply_markup=tasks_panel_keyboard(
            active_enabled=public.active_retention_enabled,
            points_enabled=public.points_renewal_enabled,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data == "admin:run_activity")
async def run_activity_check(
    callback: CallbackQuery,
    service: MembershipService,
    settings: Settings,
    scheduler: AsyncIOScheduler,
) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer("活跃检测任务已启动，请稍候…")
    result = await service.process_activity_check()
    system = await service.get_system_settings()
    await notify_activity_check_result(callback.bot, system, result)
    await callback.message.answer(
        f"✅ 活跃检测已完成，共检测 {result.total_synced} 位用户，"
        f"禁用 {len(result.disabled)} 位，删除 {len(result.deleted)} 位。"
    )
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _build_tasks_panel_text(public, scheduler),
        reply_markup=tasks_panel_keyboard(
            active_enabled=public.active_retention_enabled,
            points_enabled=public.points_renewal_enabled,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data == "admin:run_expiration")
async def run_expiration_check(
    callback: CallbackQuery,
    service: MembershipService,
    settings: Settings,
    scheduler: AsyncIOScheduler,
) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer("到期检测任务已启动，请稍候…")
    result = await run_expiration_job(service, callback.bot)
    await callback.message.answer(
        f"✅ 到期检测已完成，活跃续期 {len(result.active_renewed)} 位，"
        f"积分续期 {len(result.points_renewed)} 位，禁用 {len(result.disabled)} 位，"
        f"删除 {len(result.deleted)} 位。"
    )
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _build_tasks_panel_text(public, scheduler),
        reply_markup=tasks_panel_keyboard(
            active_enabled=public.active_retention_enabled,
            points_enabled=public.points_renewal_enabled,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data == "admin:push_leaderboard:daily")
async def push_daily_leaderboard(
    callback: CallbackQuery,
    service: MembershipService,
    settings: Settings,
    scheduler: AsyncIOScheduler,
    leaderboard_service: LeaderboardService,
) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer("每日榜推送中…")
    await run_daily_leaderboard_job(leaderboard_service, service, callback.bot, settings.timezone)
    await callback.message.answer("✅ 每日收听榜已推送到主群组")
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _build_tasks_panel_text(public, scheduler),
        reply_markup=tasks_panel_keyboard(
            active_enabled=public.active_retention_enabled,
            points_enabled=public.points_renewal_enabled,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data == "admin:push_leaderboard:weekly")
async def push_weekly_leaderboard(
    callback: CallbackQuery,
    service: MembershipService,
    settings: Settings,
    scheduler: AsyncIOScheduler,
    leaderboard_service: LeaderboardService,
) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer("每周榜推送中…")
    await run_weekly_leaderboard_job(leaderboard_service, service, callback.bot, settings.timezone)
    await callback.message.answer("✅ 每周收听榜已推送到主群组")
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _build_tasks_panel_text(public, scheduler),
        reply_markup=tasks_panel_keyboard(
            active_enabled=public.active_retention_enabled,
            points_enabled=public.points_renewal_enabled,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


# ---------------------------------------------------------------------------
# Redeem codes
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin:codes")
async def code_panel(callback: CallbackQuery, service: MembershipService, settings: Settings) -> None:
    if not await _require_admin(callback, settings):
        return
    await callback.answer()
    await replace_panel(
        callback,
        "兑换码管理",
        reply_markup=code_panel_keyboard(),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data.startswith("admin:mkcode:"))
async def ask_code_payload(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _require_admin(callback, settings):
        return
    code_type = callback.data.rsplit(":", 1)[1]
    await state.set_state(AdminStates.code_payload)
    await state.update_data(code_type=code_type)
    await callback.answer()
    await _edit_prompt_message(
        callback.message,
        "请输入兑换码参数：\n"
        "格式：days count\n"
        f"默认自动生成兑换码；count 可省略，单次最多 {MAX_REDEEM_CODES_PER_BATCH} 个。\n"
        "白名单码只需输入 count。\n"
        "示例：30 10"
    )


@router.message(AdminStates.code_payload)
async def create_code_from_payload(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not _is_admin_message(message, settings):
        await _handle_unauthorized_message(message)
        return
    data = await state.get_data()
    try:
        code_type = RedeemCodeType(data["code_type"])
        code, days, count = parse_code_payload(message.text or "", code_type)
    except KeyError:
        await message.answer("参数错误。")
        return
    except ValueError as exc:
        await message.answer(str(exc) if str(exc).startswith("格式错误") else "参数错误。")
        return
    if code is not None and count != 1:
        await message.answer("指定兑换码时一次只能创建 1 个；批量生成请使用自动生成。")
        return
    try:
        if code is None:
            redeems = await service.create_redeem_codes(
                code_type=code_type,
                days=None if not days or days <= 0 else days,
                count=count,
                created_by=message.from_user.id,
            )
        else:
            redeems = [
                await service.create_redeem_code(
                    code=code,
                    code_type=code_type,
                    days=None if not days or days <= 0 else days,
                    max_uses=1,
                    created_by=message.from_user.id,
                )
            ]
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    replies = format_created_codes_messages([redeem.code for redeem in redeems])
    for reply in replies[:-1]:
        await message.answer(reply)
    await message.answer(replies[-1], reply_markup=code_panel_keyboard())


_CODE_TYPE_LABELS = {
    "registration": "注册码",
    "renewal": "续期码",
    "whitelist": "白名单码",
}


@router.callback_query(F.data.startswith("admin:codelist:"))
async def list_codes(callback: CallbackQuery, service: MembershipService, settings: Settings) -> None:
    if not await _require_admin(callback, settings):
        return
    parts = callback.data.split(":")
    code_type_str = parts[2]
    page = int(parts[3])
    code_type = RedeemCodeType(code_type_str)
    codes = await service.list_redeem_codes(code_type=code_type, offset=page * 10, limit=10, usable=True)
    label = _CODE_TYPE_LABELS.get(code_type_str, code_type_str)
    lines = [f"{label}列表"]
    for code in codes:
        lines.append(f"<code>{html.escape(code.code)}</code>")
    await callback.answer()
    await replace_panel(
        callback,
        "\n".join(lines),
        reply_markup=code_list_keyboard(codes, code_type=code_type_str, page=page),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data.startswith("code:"))
async def code_action(callback: CallbackQuery, service: MembershipService, settings: Settings) -> None:
    if not await _require_admin(callback, settings):
        return
    parts = callback.data.split(":")
    action = parts[1]

    if action == "del":
        # code:del:{code_type}:{page}:{id}
        code_type_str, page_str, raw_id = parts[2], parts[3], parts[4]
        await service.delete_redeem_code(int(raw_id))
        await callback.answer("已删除")
    elif action == "delpage":
        # code:delpage:{code_type}:{page}
        code_type_str, page_str = parts[2], parts[3]
        page = int(page_str)
        code_type = RedeemCodeType(code_type_str)
        current = await service.list_redeem_codes(code_type=code_type, offset=page * 10, limit=10, usable=True)
        ids = [c.id for c in current]
        n = await service.delete_redeem_codes_bulk(code_type=code_type, ids=ids)
        await callback.answer(f"已删除本页 {n} 条")
    elif action == "delall":
        # code:delall:{code_type}
        code_type_str = parts[2]
        page_str = "0"
        n = await service.delete_redeem_codes_bulk(code_type=RedeemCodeType(code_type_str))
        await callback.answer(f"已删除全部 {n} 条")
    elif action == "delused":
        # code:delused:{code_type}
        code_type_str = parts[2]
        page_str = "0"
        n = await service.delete_redeem_codes_bulk(code_type=RedeemCodeType(code_type_str), used=True)
        await callback.answer(f"已删除 {n} 条已使用")
    elif action == "delunused":
        # code:delunused:{code_type}
        code_type_str = parts[2]
        page_str = "0"
        n = await service.delete_redeem_codes_bulk(code_type=RedeemCodeType(code_type_str), used=False)
        await callback.answer(f"已删除 {n} 条未使用")
    else:
        await callback.answer("未知操作")
        return

    page = int(page_str)
    code_type = RedeemCodeType(code_type_str)
    codes = await service.list_redeem_codes(code_type=code_type, offset=page * 10, limit=10, usable=True)
    label = _CODE_TYPE_LABELS.get(code_type_str, code_type_str)
    lines = [f"{label}列表"]
    for code in codes:
        lines.append(f"<code>{html.escape(code.code)}</code>")
    await replace_panel(
        callback,
        "\n".join(lines),
        reply_markup=code_list_keyboard(codes, code_type=code_type_str, page=page),
        panel_photo_path=await _panel_photo_path(service),
    )


# ---------------------------------------------------------------------------
# User list & target management
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin:users:"))
async def list_all_users(callback: CallbackQuery, service: MembershipService, settings: Settings) -> None:
    if not await _require_admin(callback, settings):
        return
    page = int(callback.data.rsplit(":", 1)[1])
    users = await service.list_users(offset=page * 10, limit=10)
    db_count, abs_count = await service.get_user_counts()
    abs_count_str = str(abs_count) if abs_count is not None else "未知"
    text = (
        "👥 用户列表\n"
        f"📊 Bot 数据库账号数：{db_count}\n"
        f"🖥️ ABS 服务器用户数：{abs_count_str}"
    )
    await callback.answer()
    await replace_panel(
        callback,
        text,
        reply_markup=users_page_keyboard(users, page=page, kind="users"),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data.startswith("admin:white:"))
async def list_white_users(callback: CallbackQuery, service: MembershipService, settings: Settings) -> None:
    if not await _require_admin(callback, settings):
        return
    page = int(callback.data.rsplit(":", 1)[1])
    users = await service.list_users(whitelisted=True, offset=page * 10, limit=10)
    await callback.answer()
    await replace_panel(
        callback,
        "白名单用户列表",
        reply_markup=users_page_keyboard(users, page=page, kind="white"),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data.startswith("admin:user:"))
async def open_target_from_list(
    callback: CallbackQuery, service: MembershipService, settings: Settings
) -> None:
    if not await _require_admin(callback, settings):
        return
    target = int(callback.data.rsplit(":", 1)[1])
    profile = await service.get_profile(target)
    await callback.answer()
    await replace_panel(
        callback,
        _target_user_text(profile),
        reply_markup=target_user_keyboard(profile, owner_id=callback.from_user.id),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.callback_query(F.data.startswith("target:"))
async def target_actions(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not await _require_admin(callback, settings):
        return
    _, raw_tgid, action = callback.data.split(":")
    target = int(raw_tgid)
    if action == "grant":
        await service.grant_registration(target, credits=1)
        bot_user = await callback.bot.get_me()
        bot_username = (getattr(bot_user, "username", "") or "").lstrip("@")
        panel_photo_path = await _panel_photo_path(service)
        is_private_admin_chat = _is_private_chat(callback.message)
        reply_markup = None
        if bot_username:
            reply_markup = registration_claim_keyboard(bot_username, target)
        try:
            await _send_registration_claim_notice(
                callback.bot,
                target,
                reply_markup=reply_markup,
                panel_photo_path=panel_photo_path,
            )
        except TelegramAPIError:
            logger.warning(
                "向用户 %s 发送注册资格通知失败",
                target,
                exc_info=True,
            )
        await callback.answer("注册资格已发放")
        panel_text = f"{_telegram_user_mention(target)} 的注册资格已发放。"
        await replace_panel(
            callback,
            panel_text,
            reply_markup=None if is_private_admin_chat else reply_markup,
            panel_photo_path=panel_photo_path,
        )
        return
    if action == "points":
        await state.set_state(AdminStates.points_delta)
        await state.update_data(target=target)
        await callback.answer()
        await _edit_prompt_message(callback.message, "请输入积分增减值，例如：10 或 -10")
        return
    if action == "expiry":
        await state.set_state(AdminStates.expiry_delta)
        await state.update_data(target=target)
        await callback.answer()
        await _edit_prompt_message(callback.message, "请输入到期时间增减天数，例如：30 或 -7")
        return
    if action == "reset":
        try:
            result = await service.reset_password(target)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        except AudiobookshelfError as exc:
            await callback.answer(f"重置失败：{_abs_user_error_message(exc)}", show_alert=True)
            return
        await callback.answer("已重置")
        await callback.message.answer(
            f"账号：<code>{html.escape(result.username)}</code>\n"
            f"新密码：<code>{html.escape(result.password)}</code>"
        )
        return
    if action == "delete":
        await service.delete_account(target)
        await callback.answer("已删除")
    elif action == "white":
        profile = await service.get_profile(target)
        new_status = not profile.is_whitelisted
        await service.set_whitelist(target, new_status)
        await callback.answer("已更新白名单")
        if new_status:
            import random
            quotes = [
                "欲知后事如何，且听下回分解！恭喜您荣登贵宾白名单，往后听书畅通无阻！",
                "说时迟，那时快！掌柜的手起笔落，已将您列入贵宾免单名册！",
                "书接上回！只听啪的一声惊堂木，掌柜的金口玉言，赐您贵宾白名单席位，请上座！",
                "花开两朵，各表一枝。今日喜报传来，您已得享贵宾白名单之礼，好生受用！",
                "古人有诗云：踏破铁鞋无觅处，得来全不费工夫。金牌在手（白名单已至），听书大吉！",
                "非是臣子多饶舌，确是主公有德声。恭喜您被特赐贵宾白名单，从此书山任君行！",
                "大江东去，浪淘尽，千古风流人物。今日这书场上，您便是免单的上宾，请慢用！",
                "天下风云出我辈，一入江湖岁月催。白名单金牌已赐，您只管安心听书！"
            ]
            quote = random.choice(quotes)
            try:
                await callback.bot.send_message(target, f"🎁【白名单权益通知】\n\n{quote}")
            except Exception as e:
                logger.warning("向用户 %s 发送白名单通知失败: %s", target, e)
    profile = await service.get_profile(target)
    await replace_panel(
        callback,
        _target_user_text(profile),
        reply_markup=target_user_keyboard(profile, owner_id=callback.from_user.id),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.message(AdminStates.points_delta)
async def adjust_points_from_payload(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not _is_admin_message(message, settings):
        await _handle_unauthorized_message(message)
        return
    data = await state.get_data()
    try:
        delta = int((message.text or "").strip())
    except ValueError:
        await message.answer("请输入数字。")
        return
    profile = await service.admin_adjust_points(data["target"], delta=delta)
    await state.clear()
    await send_panel(
        message,
        _target_user_text(profile),
        reply_markup=target_user_keyboard(profile, owner_id=message.from_user.id),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.message(AdminStates.expiry_delta)
async def adjust_expiry_from_payload(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not _is_admin_message(message, settings):
        await _handle_unauthorized_message(message)
        return
    data = await state.get_data()
    try:
        delta = int((message.text or "").strip())
    except ValueError:
        await message.answer("请输入数字。")
        return
    profile = await service.admin_adjust_expiry(data["target"], delta=delta)
    if profile.registration_credits is None:
        profile.registration_credits = 0
    await state.clear()
    await send_panel(
        message,
        _target_user_text(profile),
        reply_markup=target_user_keyboard(profile, owner_id=message.from_user.id),
        panel_photo_path=await _panel_photo_path(service),
    )


# ---------------------------------------------------------------------------
# Rebind review
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("rebind:"))
async def review_rebind_request(
    callback: CallbackQuery,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not await _require_admin(callback, settings):
        return
    try:
        _, action, raw_request_id = callback.data.split(":")
        request_id = int(raw_request_id)
    except (ValueError, AttributeError):
        await callback.answer("参数错误", show_alert=True)
        return

    try:
        if action == "approve":
            result = await service.approve_rebind_request(
                request_id,
                reviewer_telegram_id=callback.from_user.id,
            )
            notice = "换绑申请已同意"
        elif action == "reject":
            result = await service.reject_rebind_request(
                request_id,
                reviewer_telegram_id=callback.from_user.id,
            )
            notice = "换绑申请已拒绝"
        else:
            await callback.answer("参数错误", show_alert=True)
            return
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.answer(notice)
    if callback.message:
        await callback.message.edit_text(_rebind_review_text(result), reply_markup=None)
    await _notify_rebind_result(callback, result)
