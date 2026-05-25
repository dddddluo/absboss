from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
)

from absbot.config import Settings

logger = logging.getLogger(__name__)


START_COMMAND = BotCommand(command="start", description="打开用户面板")
PP_COMMAND = BotCommand(command="pp", description="打开管理面板")
SETUP_COMMAND = BotCommand(command="setup", description="初始化配置")


async def setup_bot_commands(
    bot: Bot,
    settings: Settings,
    *,
    main_group_chat_id: int | None = None,
) -> None:
    await bot.set_my_commands([START_COMMAND], scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())

    user_ids = set(settings.admin_tg_ids)
    if settings.owner_tg_id is not None:
        user_ids.add(settings.owner_tg_id)

    for user_id in sorted(user_ids):
        private_commands = [START_COMMAND, PP_COMMAND]
        if settings.is_owner(user_id):
            private_commands.append(SETUP_COMMAND)

        try:
            await bot.set_my_commands(private_commands, scope=BotCommandScopeChat(chat_id=user_id))
        except TelegramBadRequest as e:
            logger.warning("无法为用户 %d 设置私聊命令（ID 可能有误）: %s", user_id, e)
            continue

        if main_group_chat_id is not None:
            try:
                await bot.set_my_commands(
                    [PP_COMMAND],
                    scope=BotCommandScopeChatMember(chat_id=main_group_chat_id, user_id=user_id),
                )
            except TelegramBadRequest as e:
                logger.warning("无法为用户 %d 设置群组命令: %s", user_id, e)
