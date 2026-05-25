from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject

from absbot.service import MembershipService

from .helpers import _answer_membership_required, _is_group_start_command_message, is_allowed_chat_member_status, is_setup_event

logger = logging.getLogger(__name__)


class MainGroupMembershipMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if is_setup_event(event, data.get("raw_state")):
            return await handler(event, data)
        if _is_group_start_command_message(event):
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        service: MembershipService | None = data.get("service")
        bot = getattr(event, "bot", None)
        if user is None or service is None or bot is None:
            return await handler(event, data)

        system = await service.get_system_settings()
        if system.main_group_chat_id is None:
            return await handler(event, data)

        try:
            member = await bot.get_chat_member(system.main_group_chat_id, user.id)
        except TelegramAPIError:
            await _answer_membership_required(
                event,
                "无法验证入群状态，请稍后重试或联系管理员。",
            )
            return None

        if is_allowed_chat_member_status(
            getattr(member, "status", ""),
            is_member=getattr(member, "is_member", None),
        ):
            # Passively refresh TG display name (best-effort, does not block the request)
            if hasattr(member, "user") and member.user:
                try:
                    await service.update_display_name(user.id, member.user.full_name)
                except Exception:
                    pass
            return await handler(event, data)

        try:
            deleted = await service.delete_user_and_account(user.id)
        except Exception:
            logger.exception("退群清理失败 tg=%s", user.id)
            deleted = False

        if deleted:
            text = "你已退出主群组，账号和相关记录已被删除。"
        else:
            text = "你已退出主群组，无法继续使用 bot。"
        if system.main_group_link:
            text += f"\n如需重新使用，请先加入主群组：{html.escape(system.main_group_link)}"
        await _answer_membership_required(event, text)
        return None
