from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message

from absbot.config import Settings
from absbot.keyboards import admin_panel_keyboard, setup_step_keyboard
from absbot.panels import replace_panel, send_panel
from absbot.service import MembershipService

from .helpers import (
    _admin_panel_text,
    _can_run_setup,
    _handle_unauthorized_message,
    _is_private_chat,
    _panel_photo_path,
    _setup_summary_text,
)
from .states import SetupStates

logger = logging.getLogger(__name__)
router = Router()

_SETUP_STEPS = [
    SetupStates.main_group,
    SetupStates.register_days,
    SetupStates.server_lines,
    SetupStates.checkin,
    SetupStates.active_retention,
    SetupStates.points_renewal,
    SetupStates.panel_photo,
    SetupStates.rebind_review_chat,
    SetupStates.disabled_delete,
]

_SETUP_PROMPTS = {
    SetupStates.main_group: (
        "第 1 步 / 共 9 步：👥 主群组\n\n"
        "请输入主群组 ID 和群组链接，格式：<code>群组ID 群组链接</code>，"
        "例如 <code>-1001234567890 https://t.me/+abcdef</code>。"
    ),
    SetupStates.register_days: (
        "第 2 步 / 共 9 步：📅 默认注册天数\n\n"
        "请输入新账号默认有效天数（整数，至少 1）。"
    ),
    SetupStates.server_lines: (
        "第 3 步 / 共 9 步：📡 服务器线路\n\n"
        "请发送服务器线路说明（支持 HTML 格式）。"
    ),
    SetupStates.checkin: (
        "第 4 步 / 共 9 步：🎁 签到积分\n\n"
        "请输入每日签到积分范围，格式：<code>min max</code>，例如 <code>1 10</code>。\n"
        "输入 <code>off</code> 关闭签到功能。"
    ),
    SetupStates.active_retention: (
        "第 5 步 / 共 9 步：🕒 活跃保号\n\n"
        "请输入活跃保号参数，格式：<code>活跃窗口天数 续期天数</code>，例如 <code>30 30</code>。\n"
        "输入 <code>off</code> 关闭活跃保号。"
    ),
    SetupStates.points_renewal: (
        "第 6 步 / 共 9 步：💎 积分续期\n\n"
        "请输入积分续期参数，格式：<code>消耗积分 续期天数</code>，例如 <code>100 30</code>。\n"
        "输入 <code>off</code> 关闭积分续期。"
    ),
    SetupStates.panel_photo: (
        "第 7 步 / 共 9 步：🖼️ 面板图片\n\n"
        "请输入面板图片路径（本地路径或 http/https URL）。输入 <code>off</code> 使用默认图片。"
    ),
    SetupStates.rebind_review_chat: (
        "第 8 步 / 共 9 步：🔁 换绑审核群\n\n"
        "请输入换绑审核群 ID（例如 <code>-1001234567890</code>）。输入 <code>off</code> 关闭换绑申请。"
    ),
    SetupStates.disabled_delete: (
        "第 9 步 / 共 9 步：🧹 禁用后自动删除\n\n"
        "请输入账号被禁用后自动删除的等待天数。输入 <code>0</code> 关闭自动删除。"
    ),
}


def _setup_current_value_hint(step: State, public, system) -> str | None:
    if step == SetupStates.main_group:
        if system.main_group_chat_id is not None:
            return f"当前值：<code>{system.main_group_chat_id} {system.main_group_link or ''}</code>".strip()
    elif step == SetupStates.register_days:
        return f"当前值：<code>{system.default_register_days}</code>"
    elif step == SetupStates.server_lines:
        preview = public.server_lines[:60] + ("…" if len(public.server_lines) > 60 else "")
        return f"当前值：{preview}"
    elif step == SetupStates.checkin:
        if public.checkin_enabled:
            return f"当前值：<code>{public.checkin_min_points} {public.checkin_max_points}</code>"
        else:
            return "当前值：<code>off</code>"
    elif step == SetupStates.active_retention:
        if public.active_retention_enabled:
            return f"当前值：<code>{public.active_retention_window_days} {public.active_retention_extension_days}</code>"
        else:
            return "当前值：<code>off</code>"
    elif step == SetupStates.points_renewal:
        if public.points_renewal_enabled:
            return f"当前值：<code>{public.points_renewal_cost_points} {public.points_renewal_extension_days}</code>"
        else:
            return "当前值：<code>off</code>"
    elif step == SetupStates.panel_photo:
        if system.panel_photo_path:
            return f"当前值：<code>{system.panel_photo_path}</code>"
        else:
            return "当前值：默认图片"
    elif step == SetupStates.rebind_review_chat:
        if system.rebind_review_chat_id is not None:
            return f"当前值：<code>{system.rebind_review_chat_id}</code>"
        else:
            return "当前值：<code>off</code>"
    elif step == SetupStates.disabled_delete:
        return f"当前值：<code>{system.disabled_delete_after_days}</code>"
    return None


async def _setup_go_to_step(
    target: Message | CallbackQuery,
    state: FSMContext,
    step: State,
    service: MembershipService,
) -> None:
    await state.set_state(step)
    base_prompt = _SETUP_PROMPTS[step]
    public = await service.get_public_settings()
    system = await service.get_system_settings()
    hint = _setup_current_value_hint(step, public, system)
    prompt = f"{base_prompt}\n\n<i>{hint}</i>" if hint else base_prompt
    panel_photo_path = await _panel_photo_path(service)
    if isinstance(target, CallbackQuery):
        await target.answer()
        await replace_panel(target, prompt, reply_markup=setup_step_keyboard(), panel_photo_path=panel_photo_path)
    else:
        await send_panel(target, prompt, reply_markup=setup_step_keyboard(), panel_photo_path=panel_photo_path)


async def _setup_finish(
    target: Message | CallbackQuery,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    await state.clear()
    public = await service.get_public_settings()
    system = await service.get_system_settings()
    summary = _setup_summary_text(public, system)
    if isinstance(target, CallbackQuery):
        await target.answer()
        await replace_panel(
            target,
            summary,
            reply_markup=admin_panel_keyboard(
                include_start_back=True,
                is_owner=_can_run_setup(settings, target.from_user.id),
                owner_id=target.from_user.id,
            ),
            panel_photo_path=system.panel_photo_path,
        )
    else:
        await send_panel(
            target,
            summary,
            reply_markup=admin_panel_keyboard(
                include_start_back=_is_private_chat(target),
                is_owner=_can_run_setup(settings, target.from_user.id),
                owner_id=target.from_user.id,
            ),
            panel_photo_path=system.panel_photo_path,
        )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

@router.message(Command("setup"))
async def setup_command(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None or not _can_run_setup(settings, message.from_user.id):
        await _handle_unauthorized_message(message)
        return
    await state.clear()
    await _setup_go_to_step(message, state, SetupStates.main_group, service)


@router.callback_query(F.data == "admin:setup")
async def setup_start(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not _can_run_setup(settings, callback.from_user.id):
        await callback.answer("没有权限", show_alert=True)
        return
    await state.clear()
    await _setup_go_to_step(callback, state, SetupStates.main_group, service)


@router.callback_query(F.data == "setup:cancel")
async def setup_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if not _can_run_setup(settings, callback.from_user.id):
        await callback.answer("没有权限", show_alert=True)
        return
    await state.clear()
    await callback.answer("向导已中止")
    public = await service.get_public_settings()
    await replace_panel(
        callback,
        _admin_panel_text(public),
        reply_markup=admin_panel_keyboard(
            include_start_back=_is_private_chat(callback.message),
            is_owner=True,
            owner_id=callback.from_user.id,
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


# ---------------------------------------------------------------------------
# Step 1: main_group
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "setup:skip", SetupStates.main_group)
async def setup_skip_main_group(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
) -> None:
    await _setup_go_to_step(callback, state, SetupStates.register_days, service)


@router.message(SetupStates.main_group)
async def setup_set_main_group(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None or not _can_run_setup(settings, message.from_user.id):
        await _handle_unauthorized_message(message)
        return
    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("格式错误，请输入：群组ID 群组链接。")
        return
    try:
        chat_id = int(parts[0])
        await service.set_system_settings(main_group_chat_id=chat_id, main_group_link=parts[1])
    except ValueError:
        await message.answer("群组 ID 格式错误，请输入数字，例如 -1001234567890。")
        return
    await _setup_go_to_step(message, state, SetupStates.register_days, service)


# ---------------------------------------------------------------------------
# Step 2: register_days
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "setup:skip", SetupStates.register_days)
async def setup_skip_register_days(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
) -> None:
    await _setup_go_to_step(callback, state, SetupStates.server_lines, service)


@router.message(SetupStates.register_days)
async def setup_set_register_days(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None or not _can_run_setup(settings, message.from_user.id):
        await _handle_unauthorized_message(message)
        return
    try:
        days = int((message.text or "").strip())
        await service.set_system_settings(default_register_days=days)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await _setup_go_to_step(message, state, SetupStates.server_lines, service)


# ---------------------------------------------------------------------------
# Step 3: server_lines
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "setup:skip", SetupStates.server_lines)
async def setup_skip_server_lines(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
) -> None:
    await _setup_go_to_step(callback, state, SetupStates.checkin, service)


@router.message(SetupStates.server_lines)
async def setup_set_server_lines(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None or not _can_run_setup(settings, message.from_user.id):
        await _handle_unauthorized_message(message)
        return
    text = message.text or ""
    await service.set_server_lines(text)
    await _setup_go_to_step(message, state, SetupStates.checkin, service)


# ---------------------------------------------------------------------------
# Step 4: checkin
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "setup:skip", SetupStates.checkin)
async def setup_skip_checkin(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
) -> None:
    await _setup_go_to_step(callback, state, SetupStates.active_retention, service)


@router.message(SetupStates.checkin)
async def setup_set_checkin(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None or not _can_run_setup(settings, message.from_user.id):
        await _handle_unauthorized_message(message)
        return
    text = (message.text or "").strip().lower()
    if text == "off":
        public = await service.get_public_settings()
        await service.set_checkin(
            enabled=False,
            min_points=public.checkin_min_points,
            max_points=public.checkin_max_points,
        )
    else:
        parts = text.split()
        if len(parts) != 2:
            await message.answer("格式错误，请输入：min max，例如：1 10。或输入 off 关闭。")
            return
        try:
            min_points, max_points = int(parts[0]), int(parts[1])
            await service.set_checkin(enabled=True, min_points=min_points, max_points=max_points)
        except ValueError as exc:
            await message.answer(str(exc))
            return
    await _setup_go_to_step(message, state, SetupStates.active_retention, service)


# ---------------------------------------------------------------------------
# Step 5: active_retention
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "setup:skip", SetupStates.active_retention)
async def setup_skip_active_retention(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
) -> None:
    await _setup_go_to_step(callback, state, SetupStates.points_renewal, service)


@router.message(SetupStates.active_retention)
async def setup_set_active_retention(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None or not _can_run_setup(settings, message.from_user.id):
        await _handle_unauthorized_message(message)
        return
    text = (message.text or "").strip().lower()
    if text == "off":
        public = await service.get_public_settings()
        await service.set_active_retention(
            enabled=False,
            window_days=public.active_retention_window_days,
            extension_days=public.active_retention_extension_days,
        )
    else:
        parts = text.split()
        if len(parts) != 2:
            await message.answer("格式错误，请输入：活跃窗口天数 续期天数，例如：30 30。或输入 off 关闭。")
            return
        try:
            window_days, extension_days = int(parts[0]), int(parts[1])
            await service.set_active_retention(
                enabled=True, window_days=window_days, extension_days=extension_days
            )
        except ValueError as exc:
            await message.answer(str(exc))
            return
    await _setup_go_to_step(message, state, SetupStates.points_renewal, service)


# ---------------------------------------------------------------------------
# Step 6: points_renewal
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "setup:skip", SetupStates.points_renewal)
async def setup_skip_points_renewal(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    await _setup_go_to_step(callback, state, SetupStates.panel_photo, service)


@router.message(SetupStates.points_renewal)
async def setup_set_points_renewal(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None or not _can_run_setup(settings, message.from_user.id):
        await _handle_unauthorized_message(message)
        return
    text = (message.text or "").strip().lower()
    if text == "off":
        public = await service.get_public_settings()
        await service.set_points_renewal(
            enabled=False,
            cost_points=public.points_renewal_cost_points,
            extension_days=public.points_renewal_extension_days,
        )
    else:
        parts = text.split()
        if len(parts) != 2:
            await message.answer("格式错误，请输入：消耗积分 续期天数，例如：100 30。或输入 off 关闭。")
            return
        try:
            cost_points, extension_days = int(parts[0]), int(parts[1])
            await service.set_points_renewal(
                enabled=True, cost_points=cost_points, extension_days=extension_days
            )
        except ValueError as exc:
            await message.answer(str(exc))
            return
    await _setup_go_to_step(message, state, SetupStates.panel_photo, service)


# ---------------------------------------------------------------------------
# Step 7: panel_photo
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "setup:skip", SetupStates.panel_photo)
async def setup_skip_panel_photo(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
) -> None:
    await _setup_go_to_step(callback, state, SetupStates.rebind_review_chat, service)


@router.message(SetupStates.panel_photo)
async def setup_set_panel_photo(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None or not _can_run_setup(settings, message.from_user.id):
        await _handle_unauthorized_message(message)
        return
    text = (message.text or "").strip()
    panel_photo_path = None if text.lower() == "off" else text
    await service.set_system_settings(panel_photo_path=panel_photo_path)
    await _setup_go_to_step(message, state, SetupStates.rebind_review_chat, service)


# ---------------------------------------------------------------------------
# Step 8: rebind_review_chat
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "setup:skip", SetupStates.rebind_review_chat)
async def setup_skip_rebind_review_chat(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    await _setup_go_to_step(callback, state, SetupStates.disabled_delete, service)


@router.message(SetupStates.rebind_review_chat)
async def setup_set_rebind_review_chat(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None or not _can_run_setup(settings, message.from_user.id):
        await _handle_unauthorized_message(message)
        return
    text = (message.text or "").strip().lower()
    if text == "off":
        await service.set_system_settings(rebind_review_chat_id=None)
    else:
        try:
            await service.set_system_settings(rebind_review_chat_id=int(text))
        except ValueError:
            await message.answer("格式错误，请输入群 ID（例如 -1001234567890）或 off。")
            return
    await _setup_go_to_step(message, state, SetupStates.disabled_delete, service)


# ---------------------------------------------------------------------------
# Step 9: disabled_delete
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "setup:skip", SetupStates.disabled_delete)
async def setup_skip_disabled_delete(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    await _setup_finish(callback, state, service, settings)


@router.message(SetupStates.disabled_delete)
async def setup_set_disabled_delete(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None or not _can_run_setup(settings, message.from_user.id):
        await _handle_unauthorized_message(message)
        return
    try:
        days = int((message.text or "").strip())
        await service.set_system_settings(disabled_delete_after_days=days)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await _setup_finish(message, state, service, settings)
