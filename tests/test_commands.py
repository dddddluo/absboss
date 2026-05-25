from aiogram.types import (
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
)

from absbot.commands import setup_bot_commands
from absbot.config import Settings


class FakeBot:
    def __init__(self):
        self.calls = []

    async def set_my_commands(self, commands, *, scope):
        self.calls.append((scope, [command.command for command in commands]))


async def test_setup_bot_commands_sets_default_user_menus():
    bot = FakeBot()
    settings = _settings(admin_tg_ids=set(), owner_tg_id=None)

    await setup_bot_commands(bot, settings)

    assert _commands_for_scope(bot, BotCommandScopeAllPrivateChats) == ["start"]
    assert _commands_for_scope(bot, BotCommandScopeAllGroupChats) == []


async def test_setup_bot_commands_sets_admin_menus():
    bot = FakeBot()
    settings = _settings(admin_tg_ids={1001}, owner_tg_id=None)

    await setup_bot_commands(bot, settings, main_group_chat_id=-100123)

    assert _commands_for_chat(bot, 1001) == ["start", "pp"]
    assert _commands_for_chat_member(bot, -100123, 1001) == ["pp"]


async def test_setup_bot_commands_sets_owner_menus_with_setup():
    bot = FakeBot()
    settings = _settings(admin_tg_ids={1001}, owner_tg_id=1001)

    await setup_bot_commands(bot, settings, main_group_chat_id=-100123)

    assert _commands_for_chat(bot, 1001) == ["start", "pp", "setup"]
    assert _commands_for_chat_member(bot, -100123, 1001) == ["pp"]


async def test_setup_bot_commands_skips_role_group_menus_without_main_group():
    bot = FakeBot()
    settings = _settings(admin_tg_ids={1001}, owner_tg_id=2002)

    await setup_bot_commands(bot, settings, main_group_chat_id=None)

    assert _commands_for_chat(bot, 1001) == ["start", "pp"]
    assert _commands_for_chat(bot, 2002) == ["start", "pp", "setup"]
    assert not any(isinstance(scope, BotCommandScopeChatMember) for scope, _ in bot.calls)


def _settings(*, admin_tg_ids: set[int], owner_tg_id: int | None) -> Settings:
    return Settings(
        bot_token="token",
        admin_tg_ids=admin_tg_ids,
        mysql_dsn="sqlite+aiosqlite:///:memory:",
        abs_base_url="https://abs.example.com",
        abs_api_token="token",
        owner_tg_id=owner_tg_id,
    )


def _commands_for_scope(bot: FakeBot, scope_type):
    for scope, commands in bot.calls:
        if isinstance(scope, scope_type):
            return commands
    raise AssertionError(f"scope not called: {scope_type.__name__}")


def _commands_for_chat_member(bot: FakeBot, chat_id: int, user_id: int):
    for scope, commands in bot.calls:
        if isinstance(scope, BotCommandScopeChatMember) and scope.chat_id == chat_id and scope.user_id == user_id:
            return commands
    raise AssertionError(f"chat member scope not called for {chat_id}/{user_id}")


def _commands_for_chat(bot: FakeBot, chat_id: int):
    for scope, commands in bot.calls:
        if isinstance(scope, BotCommandScopeChat) and scope.chat_id == chat_id:
            return commands
    raise AssertionError(f"chat scope not called for {chat_id}")
