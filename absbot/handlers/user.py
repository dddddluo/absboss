from __future__ import annotations

import html
import logging
from datetime import date

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import asyncio

from absbot.abs_client import AudiobookshelfAuthError, AudiobookshelfError
from absbot.config import Settings
from absbot.keyboards import (
    confirm_delete_keyboard,
    lines_panel_keyboard,
    rebind_review_keyboard,
    target_user_keyboard,
    unban_confirm_keyboard,
    user_panel_keyboard,
)
from absbot.panels import replace_panel, send_panel
from absbot.service import InsufficientPointsError, MembershipService
from absbot.timeutils import format_dt

from .helpers import (
    REBIND_REVIEW_SEND_TIMEOUT_SECONDS,
    _rebind_review_text,
    _abs_user_error_message,
    _delete_group_command_message,
    _edit_prompt_message,
    _is_private_chat,
    _handle_unauthorized_message,
    _panel_photo_path,
    _registration_username_prompt,
    _replace_user_panel,
    _send_registration_username_prompt,
    _send_user_panel,
    _start_payload,
    _target_user_text,
    _try_delete_sensitive_message,
    parse_credentials,
    parse_pp_target,
    should_show_setup_notice,
)
from .states import UserStates

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("start"), F.chat.type.in_({"group", "supergroup"}))
async def cleanup_group_start_command(message: Message) -> None:
    await _delete_group_command_message(message)


@router.message(Command("start"), F.chat.type == "private")
async def start_entry(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    await state.clear()
    if message.from_user is None:
        return
    if should_show_setup_notice(
        is_initialized=await service.is_initialized(),
        is_owner=settings.is_owner(message.from_user.id),
        is_admin=settings.is_admin(message.from_user.id),
        owner_configured=settings.owner_tg_id is not None,
    ):
        await message.answer(
            "👋 欢迎使用 AudiobookshelfBot！\n\nBot 尚未初始化，请使用 /setup 指令完成初始化配置。"
        )
        return
    if _start_payload(message) == "register":
        await state.set_state(UserStates.create_username)
        await _send_registration_username_prompt(message, service)
        return
    if _start_payload(message).startswith("gift_"):
        try:
            target = int(_start_payload(message).split("_", 1)[1])
        except ValueError:
            await message.answer("抱歉，这份礼物不属于您。")
            return
        if message.from_user.id != target:
            await message.answer("抱歉，这份礼物不属于您。")
            return
        await state.set_state(UserStates.create_username)
        await _send_registration_username_prompt(message, service)
        return
    await _send_user_panel(message, service, settings, message.from_user.id)


@router.message(Command("pp"))
async def pp_entry(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    await state.clear()
    from_user = message.from_user
    if from_user is None:
        return
    target_id = parse_pp_target(message)
    if settings.is_admin(from_user.id):
        if target_id is not None:
            profile = await service.get_profile(target_id)
            await send_panel(
                message,
                _target_user_text(profile),
                reply_markup=target_user_keyboard(profile, owner_id=from_user.id),
                panel_photo_path=await _panel_photo_path(service),
            )
            return
        await message.answer("请回复用户消息发送 /pp，或发送 /pp tgid")
        return
    if not _is_private_chat(message):
        await _handle_unauthorized_message(message)
        return


@router.callback_query(F.data.startswith("close:"))
async def close_panel(callback: CallbackQuery, settings: Settings) -> None:
    owner_id = int(callback.data.split(":", 1)[1])
    if callback.from_user.id != owner_id and not settings.is_admin(callback.from_user.id):
        await callback.answer("没有权限", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("me:"))
async def user_actions(
    callback: CallbackQuery,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if callback.from_user is None:
        return
    action = callback.data.split(":", 1)[1]
    telegram_id = callback.from_user.id
    public_settings = await service.get_public_settings()
    if action == "lines":
        await callback.answer()
        reachable = await service.check_server_reachable()
        if reachable:
            status_line = "\n\n🟢 服务器在线"
            book_count = await service.get_total_book_count()
            if book_count is not None:
                status_line += f"\n📚 共 {book_count} 本有声书"
        else:
            status_line = "\n\n🔴 服务器当前离线"
        await replace_panel(
            callback,
            public_settings.server_lines + status_line,
            reply_markup=lines_panel_keyboard(),
            panel_photo_path=await _panel_photo_path(service),
            parse_mode=ParseMode.HTML,
        )
        return
    if action == "info":
        await callback.answer()
        await _replace_user_panel(callback, service, settings, telegram_id)
        return
    if action == "checkin":
        try:
            result = await service.checkin(telegram_id, today=date.today())
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            await _replace_user_panel(callback, service, settings, telegram_id)
            return
        await callback.answer(
            f"签到成功，获得 {result.awarded} 分"
            if not result.already_checked_in
            else "今天已经签到"
        )
        await _replace_user_panel(callback, service, settings, telegram_id)
        return
    if action == "create":
        await state.set_state(UserStates.create_username)
        await callback.answer()
        await replace_panel(
            callback,
            _registration_username_prompt(),
            panel_photo_path=await _panel_photo_path(service),
        )
        return
    if action == "bind":
        if not _is_private_chat(callback.message):
            await callback.answer("请在私聊中绑定账号", show_alert=True)
            return
        await state.set_state(UserStates.bind_credentials)
        await callback.answer()
        await _edit_prompt_message(
            callback.message, "请输入 Audiobookshelf 用户名和密码，格式：用户名 密码。"
        )
        return
    if action == "rebind":
        if not _is_private_chat(callback.message):
            await callback.answer("请在私聊中申请换绑", show_alert=True)
            return
        system = await service.get_system_settings()
        if system.rebind_review_chat_id is None:
            await callback.answer("未配置换绑审核群，请联系管理员", show_alert=True)
            return
        await state.set_state(UserStates.rebind_credentials)
        await callback.answer()
        await _edit_prompt_message(
            callback.message, "请输入要换绑的 Audiobookshelf 用户名和密码，格式：用户名 密码。"
        )
        return
    if action == "redeem":
        await state.set_state(UserStates.redeem_code)
        await callback.answer()
        await _edit_prompt_message(callback.message, "请输入兑换码。")
        return
    if action == "reset":
        try:
            result = await service.reset_password(telegram_id)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            await _replace_user_panel(callback, service, settings, telegram_id)
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
    if action == "unban_request":
        if not public_settings.points_unban_enabled:
            await callback.answer("积分解禁功能未开启", show_alert=True)
            return
        profile = await service.get_profile(telegram_id)
        if not profile.is_disabled:
            await callback.answer("账号未被禁用", show_alert=True)
            await _replace_user_panel(callback, service, settings, telegram_id)
            return
        cost = public_settings.points_unban_cost_points
        if (profile.points or 0) < cost:
            shortfall = cost - (profile.points or 0)
            await callback.answer(f"积分不足，还差 {shortfall} 积分", show_alert=True)
            return
        await callback.answer()
        await _edit_prompt_message(
            callback.message,
            f"解除禁用需要 {cost} 积分，确认吗？",
            reply_markup=unban_confirm_keyboard(),
        )
        return
    if action == "unban_confirm":
        if not public_settings.points_unban_enabled:
            await callback.answer("积分解禁功能未开启", show_alert=True)
            return
        try:
            await service.self_unban_by_points(telegram_id)
        except InsufficientPointsError as exc:
            await callback.answer(str(exc), show_alert=True)
            await _replace_user_panel(callback, service, settings, telegram_id)
            return
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            await _replace_user_panel(callback, service, settings, telegram_id)
            return
        await callback.answer("已解除禁用")
        await _replace_user_panel(callback, service, settings, telegram_id)
        return
    if action == "delete":
        profile = await service.get_profile(telegram_id)
        if not profile.abs_user_id:
            await callback.answer("账号不存在或已被解绑", show_alert=True)
            await _replace_user_panel(callback, service, settings, telegram_id)
            return
        await callback.answer()
        await _edit_prompt_message(
            callback.message,
            "确认注销你的 Audiobookshelf 账号？",
            reply_markup=confirm_delete_keyboard(),
        )
        return
    if action == "delete_confirm":
        try:
            await service.delete_account(telegram_id)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            await _replace_user_panel(callback, service, settings, telegram_id)
            return
        except AudiobookshelfError as exc:
            await callback.answer(f"删除失败：{_abs_user_error_message(exc)}", show_alert=True)
            return
        await callback.answer("已删除")
        await _replace_user_panel(callback, service, settings, telegram_id)


@router.message(UserStates.create_username)
async def user_create_account(
    message: Message, state: FSMContext, service: MembershipService
) -> None:
    if message.from_user is None:
        return
    username = message.text or ""
    logger.info(
        "Telegram 用户 %s 请求创建账号，用户名：%s",
        message.from_user.id,
        username,
    )
    try:
        position = await service.enqueue_registration(message.from_user.id, username)
    except ValueError as exc:
        logger.warning(
            "Telegram 用户 %s 注册入队被拒绝，用户名 %s：%s",
            message.from_user.id,
            username,
            exc,
        )
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer(
        f"✅ 已成功加入注册队列，当前排在第 {position} 位，账号创建完成后将通知您。"
    )


@router.message(UserStates.bind_credentials)
async def user_bind_account(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    try:
        username, password = parse_credentials(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await _try_delete_sensitive_message(message)
    try:
        result = await service.bind_existing_account(message.from_user.id, username, password)
    except (ValueError, AudiobookshelfAuthError) as exc:
        await message.answer(str(exc))
        return
    except AudiobookshelfError as exc:
        logger.warning(
            "为 Telegram 用户 %s 绑定 ABS 账号失败，用户名 %s：%s",
            message.from_user.id,
            username,
            exc,
        )
        await message.answer(f"验证账号失败：{html.escape(_abs_user_error_message(exc))}")
        return
    await state.clear()
    profile = await service.get_profile(message.from_user.id)
    expires_str = "♾️" if profile.is_whitelisted else format_dt(result.expires_at)
    await send_panel(
        message,
        f"绑定成功\n账号：<code>{html.escape(result.username)}</code>\n到期：{expires_str}",
        reply_markup=user_panel_keyboard(
            profile=profile,
            settings=await service.get_public_settings(),
            is_admin=settings.is_admin(message.from_user.id),
        ),
        panel_photo_path=await _panel_photo_path(service),
    )


@router.message(UserStates.rebind_credentials)
async def user_request_rebind(
    message: Message,
    state: FSMContext,
    service: MembershipService,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    system = await service.get_system_settings()
    if system.rebind_review_chat_id is None:
        await message.answer("未配置换绑审核群，请联系管理员。")
        return
    try:
        username, password = parse_credentials(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await _try_delete_sensitive_message(message)
    try:
        logger.info(
            "正在为 Telegram 用户 %s 创建换绑申请，用户名：%s",
            message.from_user.id,
            username,
        )
        request = await service.create_rebind_request(message.from_user.id, username, password)
        logger.info("换绑申请 %s 已创建", request.id)
    except (ValueError, AudiobookshelfAuthError) as exc:
        await message.answer(str(exc))
        return
    except AudiobookshelfError as exc:
        logger.warning(
            "为 Telegram 用户 %s 创建换绑申请失败，用户名 %s：%s",
            message.from_user.id,
            username,
            exc,
        )
        await message.answer(f"验证账号失败：{html.escape(_abs_user_error_message(exc))}")
        return
    try:
        logger.info(
            "正在向会话 %s 发送换绑审核消息，申请 ID：%s",
            system.rebind_review_chat_id,
            request.id,
        )
        review_message = await asyncio.wait_for(
            message.bot.send_message(
                system.rebind_review_chat_id,
                _rebind_review_text(request),
                reply_markup=rebind_review_keyboard(request.id),
            ),
            timeout=REBIND_REVIEW_SEND_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.exception("发送换绑审核消息超时，申请 ID：%s", request.id)
        await state.clear()
        await message.answer("发送换绑审核消息超时，请联系管理员。")
        return
    except TelegramAPIError as exc:
        await state.clear()
        await message.answer(f"发送换绑审核消息失败，请联系管理员：{html.escape(str(exc))}")
        return
    except Exception as exc:
        logger.exception("发送换绑审核消息失败，申请 ID：%s", request.id)
        await state.clear()
        await message.answer(f"发送换绑审核消息失败，请联系管理员：{html.escape(str(exc))}")
        return
    try:
        await service.set_rebind_review_message(
            request.id,
            chat_id=review_message.chat.id,
            message_id=review_message.message_id,
        )
    except Exception:
        logger.exception("记录换绑审核消息失败，申请 ID：%s", request.id)
        await state.clear()
        await message.answer("提交换绑申请失败，请联系管理员。")
        return
    await state.clear()
    await message.answer("换绑申请已提交，请等待管理员审核。")


@router.message(UserStates.redeem_code)
async def user_redeem_code(message: Message, state: FSMContext, service: MembershipService) -> None:
    if message.from_user is None:
        return
    try:
        result = await service.redeem_code(message.from_user.id, message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    expires_str = "♾️" if result.is_whitelisted else format_dt(result.expires_at)
    await message.answer(
        f"{html.escape(result.message)}\n"
        f"注册资格：{result.registration_credits}\n"
        f"白名单：{'是' if result.is_whitelisted else '否'}\n"
        f"积分：{result.points}\n"
        f"到期：{expires_str}"
    )
