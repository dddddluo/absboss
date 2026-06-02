import asyncio

import pytest

import absbot.handlers as handlers
import absbot.handlers.admin as admin_handlers
import absbot.keyboards as keyboards
import absbot.scheduler as scheduler
from absbot.abs_client import AudiobookshelfNotFoundError
from absbot.handlers import (
    _SETUP_PROMPTS,
    _edit_prompt_message,
    format_created_codes_messages,
    parse_code_payload,
    SetupStates,
    is_allowed_chat_member_status,
    is_private_start,
    is_setup_update,
    should_show_setup_notice,
    parse_credentials,
    parse_pp_target,
    target_actions,
    adjust_expiry_from_payload,
    set_registration_slots,
)
from absbot.keyboards import (
    admin_panel_keyboard,
    rebind_review_keyboard,
    registration_announcement_keyboard,
    setup_step_keyboard,
    target_user_keyboard,
    user_panel_keyboard,
)
from absbot.models import RedeemCodeType, TgUser
from absbot.service import (
    ExpirationProcessResult,
    ExpirationUserResult,
    PublicSettings,
    ActivityCheckResult,
    ActivityUserResult,
)


class User:
    def __init__(self, user_id, is_bot=False):
        self.id = user_id
        self.is_bot = is_bot


class Chat:
    def __init__(self, chat_type, chat_id=100):
        self.type = chat_type
        self.id = chat_id


class Message:
    def __init__(self, text, reply_user_id=None, chat_type="private", delete_fails=False):
        self.text = text
        self.chat = Chat(chat_type)
        self.reply_to_message = None
        self.delete_fails = delete_fails
        self.deleted = False
        self.delete_attempts = 0
        if reply_user_id is not None:
            self.reply_to_message = type("Reply", (), {"from_user": User(reply_user_id)})()

    async def delete(self):
        self.delete_attempts += 1
        if self.delete_fails:
            raise handlers.TelegramBadRequest(method=None, message="delete failed")
        self.deleted = True


class AdminMessage(Message):
    def __init__(self, text, *, from_user_id):
        super().__init__(text)
        self.from_user = User(from_user_id)
        self.answers = []
        self.photo_answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def answer_photo(self, photo, *, caption, reply_markup=None, parse_mode=None):
        self.photo_answers.append((photo, caption, reply_markup))


class ClearState:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


class UnauthorizedMessage(Message):
    def __init__(self, text, *, from_user_id, chat_type="supergroup", delete_fails=False):
        super().__init__(text, chat_type=chat_type, delete_fails=delete_fails)
        self.from_user = User(from_user_id)
        self.bot = RestrictBot()


class RestrictBot:
    def __init__(self, *, restrict_fails=False):
        self.restrictions = []
        self.restrict_fails = restrict_fails

    async def restrict_chat_member(self, **kwargs):
        if self.restrict_fails:
            raise handlers.TelegramBadRequest(method=None, message="not enough rights")
        self.restrictions.append(kwargs)


class Callback:
    def __init__(self, data, *, from_user_id=1):
        self.data = data
        self.from_user = User(from_user_id)
        self.message = PromptMessage()
        self.bot = FakeBotUser()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class FrozenDataCallback(Callback):
    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, value):
        if hasattr(self, "_data"):
            raise AttributeError("data is frozen")
        self._data = value


class PromptMessage:
    def __init__(self, *, has_photo=False):
        self.photo = [object()] if has_photo else []
        self.text_edits = []
        self.caption_edits = []
        self.answers = []

    async def edit_text(self, text, *, reply_markup=None):
        self.text_edits.append((text, reply_markup))

    async def edit_caption(self, *, caption, reply_markup=None):
        self.caption_edits.append((caption, reply_markup))

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class SentGroupMessage:
    def __init__(self, chat_id, message_id):
        self.chat = Chat("supergroup", chat_id)
        self.message_id = message_id


class FakeAnnouncementBot:
    def __init__(self):
        self.sent_messages = []
        self.sent_photos = []
        self.edited_messages = []
        self.edited_captions = []

    async def get_me(self):
        return type("BotUser", (), {"username": "AudiobookshelfBot"})()

    async def send_message(self, chat_id, text, *, reply_markup=None):
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": "has-keyboard" if reply_markup is not None else None,
            }
        )
        return SentGroupMessage(chat_id, 99)

    async def send_photo(self, chat_id, photo, *, caption, reply_markup=None):
        self.sent_photos.append(
            {
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
                "reply_markup": "has-keyboard" if reply_markup is not None else None,
            }
        )
        return SentGroupMessage(chat_id, 99)

    async def edit_message_text(self, *, chat_id, message_id, text, reply_markup=None):
        self.edited_messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": "has-keyboard" if reply_markup is not None else None,
            }
        )

    async def edit_message_caption(self, *, chat_id, message_id, caption, reply_markup=None):
        self.edited_captions.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": caption,
                "reply_markup": "has-keyboard" if reply_markup is not None else None,
            }
        )


class RebindMessage(Message):
    def __init__(self, text, *, from_user_id=1001):
        super().__init__(text, chat_type="private")
        self.from_user = User(from_user_id)
        self.bot = FakeAnnouncementBot()
        self.answers = []
        self.deleted = False

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def delete(self):
        self.deleted = True


class FakeBotUser:
    def __init__(self, *, send_fails=False):
        self.send_fails = send_fails
        self.sent_messages = []
        self.sent_photos = []

    async def get_me(self):
        return type("BotUser", (), {"username": "AudiobookshelfBot"})()

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode=None):
        if self.send_fails:
            raise handlers.TelegramAPIError(method=None, message="bot blocked")
        self.sent_messages.append((chat_id, text, reply_markup, parse_mode))

    async def send_photo(self, chat_id, photo, *, caption, reply_markup=None, parse_mode=None):
        if self.send_fails:
            raise handlers.TelegramAPIError(method=None, message="bot blocked")
        self.sent_photos.append((chat_id, photo, caption, reply_markup, parse_mode))


class FakeSystemSettings:
    def __init__(self, *, main_group_chat_id=None, main_group_link=None, panel_photo_path=None):
        self.main_group_chat_id = main_group_chat_id
        self.main_group_link = main_group_link
        self.panel_photo_path = panel_photo_path


class MembershipCheckBot:
    def __init__(self):
        self.chat_member_checks = []

    async def get_chat_member(self, chat_id, user_id):
        self.chat_member_checks.append((chat_id, user_id))
        return type("Member", (), {"status": "left"})()


class MembershipCheckMessage(Message):
    def __init__(self, text, *, chat_type="supergroup"):
        super().__init__(text, chat_type=chat_type)
        self.from_user = User(2002)
        self.bot = MembershipCheckBot()
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class MembershipCheckService:
    def __init__(self):
        self.system_settings_calls = 0

    async def get_system_settings(self):
        self.system_settings_calls += 1
        return FakeSystemSettings(main_group_chat_id=-100123)


class FakeAnnouncementService:
    def __init__(
        self, *, public_settings, system_settings, announcement_message=(None, None, False)
    ):
        self.public_settings = public_settings
        self.system_settings = system_settings
        if len(announcement_message) == 2:
            self.announcement_message = (announcement_message[0], announcement_message[1], False)
        else:
            self.announcement_message = announcement_message

    async def get_public_settings(self):
        return self.public_settings

    async def get_system_settings(self):
        return self.system_settings

    async def get_registration_announcement_message(self):
        return self.announcement_message

    async def set_registration_announcement_message(self, *, chat_id, message_id, is_open=False):
        self.announcement_message = (chat_id, message_id, is_open)


class FakeRegistrationSlotService:
    def __init__(self):
        self.registration_calls = []

    async def set_registration(self, *, opened, slots):
        self.registration_calls.append((opened, slots))

    async def get_system_settings(self):
        return FakeSystemSettings(main_group_chat_id=-100123)

    async def get_public_settings(self):
        return _settings(
            registration_open=bool(self.registration_calls and self.registration_calls[-1][0])
        )


class FakeCreateAccountService:
    def __init__(
        self, *, public_settings, public_settings_after=None, create_error=None, enqueue_error=None
    ):
        self.public_settings = public_settings
        self.public_settings_after = public_settings_after or public_settings
        self.public_settings_calls = 0
        self.create_error = create_error
        self.enqueue_error = enqueue_error
        self.enqueued = []

    async def enqueue_registration(self, telegram_id, abs_username):
        if self.enqueue_error is not None:
            raise self.enqueue_error
        self.enqueued.append((telegram_id, abs_username))
        return 1

    async def create_account_from_registration(self, telegram_id, username):
        if self.create_error is not None:
            raise self.create_error
        return type(
            "AccountCreationResult",
            (),
            {
                "username": username,
                "initial_password": "secret",
                "expires_at": None,
            },
        )()

    async def get_public_settings(self):
        self.public_settings_calls += 1
        if self.public_settings_calls == 1:
            return self.public_settings
        return self.public_settings_after


class FakeExpirationCheckService:
    def __init__(self, result):
        self.result = result

    async def process_expiration_enforcement(self, *args, **kwargs):
        return self.result

    async def process_points_renewals(self, *args, **kwargs):
        return self.result

    async def get_system_settings(self):
        return FakeSystemSettings(main_group_chat_id=None)

    async def get_public_settings(self):
        return _settings()


class FakeActivityCheckService:
    def __init__(self, result):
        self.result = result

    async def process_active_renewals(self, *args, **kwargs):
        return self.result

    async def get_system_settings(self):
        return FakeSystemSettings(main_group_chat_id=None)

    async def get_public_settings(self):
        return _settings()


class FakeClearUsersService:
    def __init__(self):
        self.clear_calls = 0
        self.list_calls = []

    async def clear_all_users(self):
        self.clear_calls += 1
        return 2, 3

    async def list_users(self, *, offset=0, limit=10, **kwargs):
        self.list_calls.append((offset, limit, kwargs))
        return []

    async def get_user_counts_extended(self):
        return 0, 1, 1

    async def get_system_settings(self):
        return FakeSystemSettings(main_group_chat_id=None)


class FakeScheduler:
    def get_job(self, job_id):
        return None


def _expiration_user(telegram_id, username):
    return ExpirationUserResult(
        telegram_id=telegram_id,
        abs_user_id=f"abs-{telegram_id}",
        abs_username=username,
        expires_at=None,
    )


class Update:
    def __init__(self, message=None, callback_query=None):
        self.message = message
        self.callback_query = callback_query


def test_parse_pp_target_from_command_argument():
    assert parse_pp_target(Message("/pp 123456")) == 123456


def test_parse_pp_target_from_reply_message():
    assert parse_pp_target(Message("/pp", reply_user_id=789)) == 789


def test_parse_pp_target_ignores_plain_text_pp():
    assert parse_pp_target(Message("pp 123456")) is None


@pytest.mark.asyncio
async def test_admin_pp_without_target_prompts_for_target(monkeypatch):
    async def fail_send_panel(*args, **kwargs):
        raise AssertionError("admin panel should not open without a target")

    async def fake_admin_panel_text(service):
        return "admin panel"

    monkeypatch.setattr(handlers, "send_panel", fail_send_panel)
    monkeypatch.setattr(handlers, "_admin_panel_text", fake_admin_panel_text)
    message = AdminMessage("/pp", from_user_id=1001)

    await handlers.pp_entry(
        message,
        ClearState(),
        FakeTargetService(),
        FakeSettings(admin_ids={1001}),
    )

    assert message.answers == [("请回复用户消息发送 /pp，或发送 /pp tgid", {})]


@pytest.mark.asyncio
async def test_unauthorized_group_admin_state_message_is_deleted_and_muted():
    message = UnauthorizedMessage("10", from_user_id=2002, chat_type="supergroup")

    await set_registration_slots(
        message, ClearState(), FakeTargetService(), FakeSettings(admin_ids={1001})
    )

    assert message.deleted is True
    assert len(message.bot.restrictions) == 1
    restriction = message.bot.restrictions[0]
    assert restriction["chat_id"] == 100
    assert restriction["user_id"] == 2002
    assert restriction["permissions"].can_send_messages is False
    assert restriction["until_date"] is not None


@pytest.mark.asyncio
async def test_unauthorized_private_admin_state_message_ignores_delete_failure_and_skips_mute():
    message = UnauthorizedMessage(
        "10",
        from_user_id=2002,
        chat_type="private",
        delete_fails=True,
    )

    await set_registration_slots(
        message, ClearState(), FakeTargetService(), FakeSettings(admin_ids={1001})
    )

    assert message.deleted is False
    assert message.delete_attempts == 1
    assert message.bot.restrictions == []


@pytest.mark.asyncio
async def test_unauthorized_group_logs_failed_mute(caplog):
    message = UnauthorizedMessage("10", from_user_id=2002, chat_type="supergroup")
    message.bot = RestrictBot(restrict_fails=True)

    await set_registration_slots(
        message, ClearState(), FakeTargetService(), FakeSettings(admin_ids={1001})
    )

    assert "failed to restrict unauthorized user" in caplog.text or "禁言未授权用户" in caplog.text
    assert "2002" in caplog.text


@pytest.mark.asyncio
async def test_non_admin_group_pp_is_deleted_and_muted(monkeypatch):
    async def fail_send_user_panel(*args, **kwargs):
        raise AssertionError("non-admin group /pp should not open the user panel")

    monkeypatch.setattr(handlers, "_send_user_panel", fail_send_user_panel)
    message = UnauthorizedMessage("/pp", from_user_id=2002, chat_type="supergroup")

    await handlers.pp_entry(
        message, ClearState(), FakeTargetService(), FakeSettings(admin_ids={1001})
    )

    assert message.deleted is True
    assert len(message.bot.restrictions) == 1
    restriction = message.bot.restrictions[0]
    assert restriction["chat_id"] == 100
    assert restriction["user_id"] == 2002
    assert restriction["permissions"].can_send_messages is False


@pytest.mark.asyncio
async def test_private_non_admin_pp_opens_user_panel_without_group_penalty(monkeypatch):
    sent_user_panels = []

    async def fake_send_user_panel(message_arg, service_arg, settings_arg, telegram_id):
        sent_user_panels.append((message_arg, service_arg, settings_arg, telegram_id))

    monkeypatch.setattr(handlers, "_send_user_panel", fake_send_user_panel)
    message = UnauthorizedMessage("/pp", from_user_id=2002, chat_type="private")
    state = ClearState()
    service = FakeTargetService()
    settings = FakeSettings(admin_ids={1001})

    await handlers.pp_entry(message, state, service, settings)

    assert sent_user_panels == []
    assert message.deleted is False
    assert message.delete_attempts == 0
    assert message.bot.restrictions == []


@pytest.mark.asyncio
async def test_admin_group_pp_with_target_opens_target_panel_without_group_penalty(monkeypatch):
    sent_panels = []

    async def fake_send_panel(message_arg, caption, *, reply_markup=None, panel_photo_path=None):
        sent_panels.append((message_arg, caption, reply_markup, panel_photo_path))

    monkeypatch.setattr(handlers, "send_panel", fake_send_panel)
    message = UnauthorizedMessage("/pp 2002", from_user_id=1001, chat_type="supergroup")
    service = FakeTargetService()

    await handlers.pp_entry(message, ClearState(), service, FakeSettings(admin_ids={1001}))

    assert len(sent_panels) == 1
    sent_message, caption, reply_markup, panel_photo_path = sent_panels[0]
    assert sent_message is message
    assert caption == handlers._target_user_text(TgUser(telegram_id=2002, registration_credits=1))
    assert reply_markup is not None
    assert panel_photo_path is None
    assert message.deleted is False
    assert message.delete_attempts == 0
    assert message.bot.restrictions == []


def test_private_start_is_user_panel_entry():
    assert is_private_start(Message("/start")) is True
    assert is_private_start(Message("/start@AudiobookshelfBot")) is True


@pytest.mark.asyncio
async def test_private_plain_start_opens_user_panel_for_requesting_user(monkeypatch):
    class Service(FakeTargetService):
        async def is_initialized(self):
            return True

    sent_user_panels = []

    async def fake_send_user_panel(message_arg, service_arg, settings_arg, telegram_id):
        sent_user_panels.append((message_arg, service_arg, settings_arg, telegram_id))

    monkeypatch.setattr(handlers, "_send_user_panel", fake_send_user_panel)
    message = AdminMessage("/start", from_user_id=2002)
    state = ClearState()
    service = Service()
    settings = FakeSettings(admin_ids={1001})

    await handlers.start_entry(message, state, service, settings)

    assert sent_user_panels == [(message, service, settings, 2002)]
    assert state.cleared is True


def test_group_start_is_not_user_panel_entry():
    assert is_private_start(Message("/start", chat_type="group")) is False


@pytest.mark.asyncio
async def test_group_start_command_is_deleted():
    message = Message("/start", chat_type="supergroup")

    await handlers.cleanup_group_start_command(message)

    assert message.deleted is True
    assert message.delete_attempts == 1


@pytest.mark.asyncio
async def test_group_start_command_with_bot_username_is_deleted():
    message = Message("/start@AudiobookshelfBot", chat_type="group")

    await handlers.cleanup_group_start_command(message)

    assert message.deleted is True
    assert message.delete_attempts == 1


@pytest.mark.asyncio
async def test_group_start_delete_failure_is_logged(caplog):
    message = Message("/start", chat_type="supergroup", delete_fails=True)

    await handlers.cleanup_group_start_command(message)

    assert message.deleted is False
    assert message.delete_attempts == 1
    assert "删除群组指令消息失败" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/start", "/start@AudiobookshelfBot"])
async def test_group_start_bypasses_membership_middleware(command):
    middleware = handlers.MainGroupMembershipMiddleware()
    message = MembershipCheckMessage(command, chat_type="supergroup")
    service = MembershipCheckService()
    handled = []

    async def handler(event, data):
        handled.append((event, data))
        return "handled"

    result = await middleware(handler, message, {"service": service})

    assert result == "handled"
    assert handled == [(message, {"service": service})]
    assert service.system_settings_calls == 0
    assert message.bot.chat_member_checks == []
    assert message.answers == []


@pytest.mark.asyncio
async def test_bot_user_bypasses_membership_middleware():
    middleware = handlers.MainGroupMembershipMiddleware()
    message = MembershipCheckMessage("/any_command", chat_type="supergroup")
    message.from_user = User(1087968824, is_bot=True)
    service = MembershipCheckService()
    handled = []

    async def handler(event, data):
        handled.append((event, data))
        return "handled"

    result = await middleware(handler, message, {"service": service})

    assert result == "handled"
    assert handled == [(message, {"service": service})]
    assert service.system_settings_calls == 0
    assert message.bot.chat_member_checks == []
    assert message.answers == []


@pytest.mark.asyncio
async def test_automatic_forward_bypasses_membership_middleware():
    middleware = handlers.MainGroupMembershipMiddleware()
    message = MembershipCheckMessage("/any_command", chat_type="supergroup")
    message.is_automatic_forward = True
    service = MembershipCheckService()
    handled = []

    async def handler(event, data):
        handled.append((event, data))
        return "handled"

    result = await middleware(handler, message, {"service": service})

    assert result == "handled"
    assert handled == [(message, {"service": service})]
    assert service.system_settings_calls == 0
    assert message.bot.chat_member_checks == []


@pytest.mark.asyncio
async def test_sender_chat_bypasses_membership_middleware():
    middleware = handlers.MainGroupMembershipMiddleware()
    message = MembershipCheckMessage("/any_command", chat_type="supergroup")
    message.sender_chat = type("Chat", (), {"type": "channel", "id": -100987})()
    service = MembershipCheckService()
    handled = []

    async def handler(event, data):
        handled.append((event, data))
        return "handled"

    result = await middleware(handler, message, {"service": service})

    assert result == "handled"
    assert handled == [(message, {"service": service})]
    assert service.system_settings_calls == 0
    assert message.bot.chat_member_checks == []


@pytest.mark.asyncio
async def test_service_user_bypasses_membership_middleware():
    middleware = handlers.MainGroupMembershipMiddleware()
    message = MembershipCheckMessage("/any_command", chat_type="supergroup")
    message.from_user = User(777000, is_bot=False)
    service = MembershipCheckService()
    handled = []

    async def handler(event, data):
        handled.append((event, data))
        return "handled"

    result = await middleware(handler, message, {"service": service})

    assert result == "handled"
    assert handled == [(message, {"service": service})]
    assert service.system_settings_calls == 0
    assert message.bot.chat_member_checks == []


def test_uninitialized_start_notice_is_shown_to_owner_or_admin():
    assert (
        should_show_setup_notice(
            is_initialized=False, is_owner=True, is_admin=False, owner_configured=True
        )
        is True
    )
    assert (
        should_show_setup_notice(
            is_initialized=False, is_owner=False, is_admin=True, owner_configured=False
        )
        is False
    )
    assert (
        should_show_setup_notice(
            is_initialized=False, is_owner=False, is_admin=True, owner_configured=True
        )
        is False
    )
    assert (
        should_show_setup_notice(
            is_initialized=False, is_owner=False, is_admin=False, owner_configured=False
        )
        is False
    )
    assert (
        should_show_setup_notice(
            is_initialized=True, is_owner=True, is_admin=True, owner_configured=True
        )
        is False
    )


def test_start_keyboard_for_user_without_account_hides_account_actions():
    keyboard = user_panel_keyboard(
        profile=TgUser(telegram_id=1001),
        settings=_settings(checkin_enabled=True),
        is_admin=False,
    )
    texts = _button_texts(keyboard)

    assert "👤 个人信息" not in texts
    assert "🆕 创建账号" in texts
    assert "🔗 绑定账号" in texts
    assert "🔄 申请换绑" in texts
    assert "🎟️ 兑换码" in texts
    assert "📡 查看线路" not in texts
    assert "🎁 每日签到" in texts
    assert "🔐 重置密码" not in texts
    assert "🗑️ 注销账号" not in texts
    assert "🕒 活跃时间" not in texts
    assert "⚙️ 管理面板" not in texts


def test_start_keyboard_for_user_with_account_shows_account_actions():
    keyboard = user_panel_keyboard(
        profile=TgUser(telegram_id=1002, abs_user_id="usr_1", abs_username="alice"),
        settings=_settings(checkin_enabled=False),
        is_admin=False,
    )
    texts = _button_texts(keyboard)

    assert "🔐 重置密码" in texts
    assert "🗑️ 注销账号" in texts
    assert "🕒 活跃时间" not in texts
    assert "🔗 绑定账号" not in texts
    assert "🔄 申请换绑" not in texts
    assert "🎁 今日听赏" not in texts


def test_user_panel_text_does_not_show_combined_activity():
    text = handlers._user_panel_text(
        TgUser(
            telegram_id=1005,
            abs_user_id="usr_1",
            abs_username="alice",
        )
    )

    assert "最近登录" in text
    assert "最近播放" in text
    assert "综合活跃" not in text


def test_user_panel_text_hides_account_dates_without_abs_account():
    text = handlers._user_panel_text(TgUser(telegram_id=1006))

    assert "到期" not in text
    assert "最近登录" not in text
    assert "最近播放" not in text


def test_user_panel_text_shows_infinity_for_whitelisted_user():
    text = handlers._user_panel_text(
        TgUser(
            telegram_id=1007,
            abs_user_id="usr_2",
            abs_username="bob",
            is_whitelisted=True,
        )
    )
    assert "⏳ 到期：♾️" in text


def test_target_user_text_hides_expiration_without_abs_account():
    text = handlers._target_user_text(TgUser(telegram_id=1008))

    assert "到期" not in text


def test_target_user_text_shows_infinity_for_whitelisted_user():
    text = handlers._target_user_text(
        TgUser(
            telegram_id=1009,
            abs_user_id="usr_3",
            abs_username="charlie",
            is_whitelisted=True,
        )
    )
    assert "⏳ 到期：♾️" in text


def test_target_keyboard_without_abs_account_hides_account_management_actions():
    keyboard = target_user_keyboard(TgUser(telegram_id=1008), owner_id=9999)
    texts = _button_texts(keyboard)

    assert "🔐 重置密码" not in texts
    assert "🗑️ 删除用户" not in texts
    assert "⭐ 赠送白名单" not in texts
    assert "⏰ 调整到期时间" not in texts


def test_target_keyboard_with_abs_account_shows_expiry_adjust_button():
    keyboard = target_user_keyboard(
        TgUser(
            telegram_id=1010,
            abs_user_id="usr_1",
            abs_username="alice",
            registration_credits=0,
        ),
        owner_id=9999,
    )
    callbacks = _button_callbacks(keyboard)

    assert callbacks["⏰ 调整到期时间"] == "target:1010:expiry"


def test_target_keyboard_with_account_and_registration_credit_hides_grant_button():
    keyboard = target_user_keyboard(
        TgUser(telegram_id=1009, abs_user_id="usr_1", registration_credits=1), owner_id=9999
    )
    texts = _button_texts(keyboard)

    assert "🎁 赠送注册资格" not in texts


def test_start_keyboard_for_admin_has_management_entry():
    keyboard = user_panel_keyboard(
        profile=TgUser(telegram_id=1003),
        settings=_settings(checkin_enabled=True),
        is_admin=True,
    )
    texts = _button_texts(keyboard)

    assert "⚙️ 管理面板" in texts


def test_user_panel_keyboard_can_hide_close_button_for_private_chat():
    keyboard = user_panel_keyboard(
        profile=TgUser(telegram_id=1007),
        settings=_settings(checkin_enabled=True),
        is_admin=False,
        include_close=False,
    )
    texts = _button_texts(keyboard)

    assert "❌ 关闭" not in texts


def test_admin_keyboard_has_emoji_and_back_to_start():
    keyboard = admin_panel_keyboard(include_start_back=True, owner_id=9999)
    texts = _button_texts(keyboard)

    assert "📝 开放注册" in texts
    assert "⬅️ 返回主面板" in texts
    assert "🚀 初始化向导" not in texts


def test_admin_keyboard_shows_setup_wizard_for_owner():
    keyboard = admin_panel_keyboard(include_start_back=True, is_owner=True, owner_id=9999)
    texts = _button_texts(keyboard)

    assert "🚀 初始化向导" in texts


def test_admin_keyboard_hides_setup_wizard_for_non_owner():
    keyboard = admin_panel_keyboard(include_start_back=True, is_owner=False, owner_id=9999)
    texts = _button_texts(keyboard)

    assert "🚀 初始化向导" not in texts


def test_setup_step_keyboard_has_skip_and_cancel():
    keyboard = setup_step_keyboard()
    callbacks = _button_callbacks(keyboard)

    assert "⏭️ 跳过" in callbacks
    assert callbacks["⏭️ 跳过"] == "setup:skip"
    assert "🚫 中止向导" in callbacks
    assert callbacks["🚫 中止向导"] == "setup:cancel"


def test_setup_first_step_configures_main_group_not_registration():
    prompt = _SETUP_PROMPTS[SetupStates.main_group]

    assert "主群组" in prompt
    assert "群组链接" in prompt
    assert "注册名额" not in prompt


def test_setup_wizard_has_no_scheduler_step():
    prompt_text = "\n".join(_SETUP_PROMPTS.values())

    assert not hasattr(SetupStates, "scheduler")
    assert "定时任务" not in prompt_text


def test_setup_prompts_include_disabled_delete_step():
    assert SetupStates.disabled_delete in _SETUP_PROMPTS
    assert "第 9 步 / 共 9 步" in _SETUP_PROMPTS[SetupStates.disabled_delete]
    assert "0" in _SETUP_PROMPTS[SetupStates.disabled_delete]


def test_setup_summary_displays_disabled_delete_setting():
    public = _settings()
    system = type(
        "System",
        (),
        {
            "main_group_chat_id": -100123,
            "main_group_link": "https://t.me/+abc",
            "default_register_days": 30,
            "panel_photo_path": None,
            "rebind_review_chat_id": None,
            "disabled_delete_after_days": 5,
        },
    )()

    text = handlers._setup_summary_text(public, system)

    assert "自动删除：禁用 5 天后" in text


async def test_setup_set_disabled_delete_saves_days_and_finishes(monkeypatch):
    class Service:
        def __init__(self):
            self.settings_calls = []

        async def set_system_settings(self, **kwargs):
            self.settings_calls.append(kwargs)

    class OwnerSettings:
        def is_owner(self, telegram_id):
            return telegram_id == 1001

    finished = []

    async def fake_setup_finish(message, state, service, settings):
        finished.append((message, state, service, settings))

    monkeypatch.setattr(handlers, "_setup_finish", fake_setup_finish)
    message = AdminMessage("5", from_user_id=1001)
    state = FakeState()
    service = Service()
    settings = OwnerSettings()

    await handlers.setup_set_disabled_delete(message, state, service, settings)

    assert service.settings_calls == [{"disabled_delete_after_days": 5}]
    assert finished == [(message, state, service, settings)]
    assert message.answers == []


def test_setup_update_is_exempt_from_membership_check():
    assert is_setup_update(Update(message=Message("/setup"))) is True
    assert is_setup_update(Update(callback_query=Callback("admin:setup"))) is True
    assert is_setup_update(Update(callback_query=Callback("setup:skip"))) is True
    assert is_setup_update(Update(message=Message("/pp"))) is False


def test_allowed_chat_member_statuses():
    assert is_allowed_chat_member_status("member") is True
    assert is_allowed_chat_member_status("administrator") is True
    assert is_allowed_chat_member_status("creator") is True
    assert is_allowed_chat_member_status("left") is False
    assert is_allowed_chat_member_status("kicked") is False


def test_bind_and_rebind_buttons_have_callback_data():
    keyboard = user_panel_keyboard(
        profile=TgUser(telegram_id=1004),
        settings=_settings(checkin_enabled=False),
        is_admin=False,
    )
    callbacks = _button_callbacks(keyboard)

    assert callbacks["🔗 绑定账号"] == "me:bind"
    assert callbacks["🔄 申请换绑"] == "me:rebind"


def test_rebind_review_keyboard_has_admin_actions():
    keyboard = rebind_review_keyboard(12)
    callbacks = _button_callbacks(keyboard)

    assert callbacks["✅ 同意换绑"] == "rebind:approve:12"
    assert callbacks["❌ 拒绝换绑"] == "rebind:reject:12"


def test_registration_announcement_keyboard_links_to_start_register():
    keyboard = registration_announcement_keyboard("AudiobookshelfBot")
    urls = _button_urls(keyboard)

    assert urls["🚀 开始注册"] == "https://t.me/AudiobookshelfBot?start=register"


def test_registration_claim_keyboard_links_to_start_register():
    keyboard = keyboards.registration_claim_keyboard("AudiobookshelfBot", 1001)
    urls = _button_urls(keyboard)

    assert urls["🎁 领取注册资格"] == "https://t.me/AudiobookshelfBot?start=gift_1001"


def test_registration_announcement_text_reflects_open_and_closed_state():
    opened = _settings(registration_open=True, registration_slots=3)
    closed = _settings(registration_open=False, registration_slots=0)

    assert "开放注册中" in scheduler.registration_announcement_text(opened)
    assert "名额：3" in scheduler.registration_announcement_text(opened)
    assert "注册已关闭" in scheduler.registration_announcement_text(closed)
    assert "开放注册中" not in scheduler.registration_announcement_text(closed)


async def test_sync_registration_announcement_sends_and_stores_message():
    from aiogram.types import FSInputFile
    from absbot.panels import DEFAULT_PANEL_PHOTO_PATH

    service = FakeAnnouncementService(
        public_settings=_settings(registration_open=True, registration_slots=2),
        system_settings=FakeSystemSettings(main_group_chat_id=-100123),
    )
    bot = FakeAnnouncementBot()

    await scheduler.sync_registration_announcement(bot, service)

    assert len(bot.sent_photos) == 1
    photo_entry = bot.sent_photos[0]
    assert photo_entry["chat_id"] == -100123
    assert isinstance(photo_entry["photo"], FSInputFile)
    assert photo_entry["photo"].path == DEFAULT_PANEL_PHOTO_PATH
    assert photo_entry["caption"] == scheduler.registration_announcement_text(
        service.public_settings
    )
    assert photo_entry["reply_markup"] == "has-keyboard"
    assert bot.sent_messages == []
    assert service.announcement_message == (-100123, 99, True)


async def test_sync_registration_announcement_sends_photo_with_caption():
    service = FakeAnnouncementService(
        public_settings=_settings(registration_open=True, registration_slots=2),
        system_settings=FakeSystemSettings(
            main_group_chat_id=-100123,
            panel_photo_path="https://example.com/register.jpg",
        ),
    )
    bot = FakeAnnouncementBot()

    await scheduler.sync_registration_announcement(bot, service)

    assert bot.sent_photos == [
        {
            "chat_id": -100123,
            "photo": "https://example.com/register.jpg",
            "caption": scheduler.registration_announcement_text(service.public_settings),
            "reply_markup": "has-keyboard",
        }
    ]
    assert bot.sent_messages == []
    assert service.announcement_message == (-100123, 99, True)


async def test_sync_registration_announcement_edits_closed_message():
    service = FakeAnnouncementService(
        public_settings=_settings(registration_open=False, registration_slots=0),
        system_settings=FakeSystemSettings(main_group_chat_id=-100123),
        announcement_message=(-100123, 88),
    )
    bot = FakeAnnouncementBot()

    await scheduler.sync_registration_announcement(bot, service)

    assert bot.edited_captions == [
        {
            "chat_id": -100123,
            "message_id": 88,
            "caption": scheduler.registration_announcement_text(service.public_settings),
            "reply_markup": None,
        }
    ]
    assert bot.edited_messages == []
    assert bot.sent_messages == []


async def test_sync_registration_announcement_edits_photo_caption_when_configured():
    service = FakeAnnouncementService(
        public_settings=_settings(registration_open=False, registration_slots=0),
        system_settings=FakeSystemSettings(
            main_group_chat_id=-100123,
            panel_photo_path="https://example.com/register.jpg",
        ),
        announcement_message=(-100123, 88),
    )
    bot = FakeAnnouncementBot()

    await scheduler.sync_registration_announcement(bot, service)

    assert bot.edited_captions == [
        {
            "chat_id": -100123,
            "message_id": 88,
            "caption": scheduler.registration_announcement_text(service.public_settings),
            "reply_markup": None,
        }
    ]
    assert bot.edited_messages == []


async def test_sync_registration_announcement_closed_to_open_sends_new_message():
    service = FakeAnnouncementService(
        public_settings=_settings(registration_open=True, registration_slots=5),
        system_settings=FakeSystemSettings(main_group_chat_id=-100123),
        announcement_message=(-100123, 88, False),
    )
    bot = FakeAnnouncementBot()

    await scheduler.sync_registration_announcement(bot, service)

    # Since it transitioned from closed to open, it must send a new photo message
    assert len(bot.sent_photos) == 1
    assert bot.sent_photos[0]["chat_id"] == -100123
    # It must not edit the existing message
    assert bot.edited_captions == []
    # And the stored announcement message is updated to the new message ID and is_open=True
    assert service.announcement_message == (-100123, 99, True)


async def test_sync_registration_announcement_open_to_open_edits_message():
    service = FakeAnnouncementService(
        public_settings=_settings(registration_open=True, registration_slots=4),
        system_settings=FakeSystemSettings(main_group_chat_id=-100123),
        announcement_message=(-100123, 88, True),
    )
    bot = FakeAnnouncementBot()

    await scheduler.sync_registration_announcement(bot, service)

    # Since it is open to open, it should edit in place
    assert bot.edited_captions == [
        {
            "chat_id": -100123,
            "message_id": 88,
            "caption": scheduler.registration_announcement_text(service.public_settings),
            "reply_markup": "has-keyboard",
        }
    ]
    assert bot.sent_messages == []
    assert len(bot.sent_photos) == 0
    assert service.announcement_message == (-100123, 88, True)


async def test_sync_registration_announcement_open_to_closed_edits_message():
    service = FakeAnnouncementService(
        public_settings=_settings(registration_open=False, registration_slots=0),
        system_settings=FakeSystemSettings(main_group_chat_id=-100123),
        announcement_message=(-100123, 88, True),
    )
    bot = FakeAnnouncementBot()

    await scheduler.sync_registration_announcement(bot, service)

    # Since it is open to closed, it should edit in place
    assert bot.edited_captions == [
        {
            "chat_id": -100123,
            "message_id": 88,
            "caption": scheduler.registration_announcement_text(service.public_settings),
            "reply_markup": None,
        }
    ]
    assert bot.sent_messages == []
    assert len(bot.sent_photos) == 0
    assert service.announcement_message == (-100123, 88, False)


async def test_set_registration_slots_syncs_group_announcement(monkeypatch):
    service = FakeRegistrationSlotService()
    state = ClearState()
    message = AdminMessage("2", from_user_id=1001)
    message.bot = object()
    synced = []

    async def fake_sync(bot, service_arg, *, alert_target=None):
        synced.append((bot, service_arg, alert_target))

    def fake_admin_panel_text(public):
        return "admin panel"

    async def fake_send_panel(*args, **kwargs):
        return None

    monkeypatch.setattr(handlers, "sync_registration_announcement", fake_sync)
    monkeypatch.setattr(handlers, "_admin_panel_text", fake_admin_panel_text)
    monkeypatch.setattr(handlers, "send_panel", fake_send_panel)

    await set_registration_slots(message, state, service, FakeSettings(admin_ids={1001}))

    assert service.registration_calls == [(True, 2)]
    assert state.cleared is True
    assert synced == [(message.bot, service, message)]


async def test_user_create_account_enqueues_registration():
    service = FakeCreateAccountService(
        public_settings=_settings(registration_open=True, registration_slots=1)
    )
    state = ClearState()
    message = AdminMessage("alice", from_user_id=1001)

    await handlers.user_create_account(message, state, service)

    assert service.enqueued == [(1001, "alice")]
    assert state.cleared is True
    sent_text = message.answers[0][0]
    assert "已成功加入注册队列" in sent_text
    assert "第 1 位" in sent_text


async def test_user_create_account_logs_value_error_rejection(caplog):
    service = FakeCreateAccountService(
        public_settings=_settings(registration_open=True, registration_slots=1),
        enqueue_error=ValueError("用户名已存在"),
    )
    state = ClearState()
    message = AdminMessage("alice", from_user_id=1001)

    await handlers.user_create_account(message, state, service)

    assert message.answers == [("用户名已存在", {})]
    assert "registration enqueue rejected" in caplog.text or "注册入队被拒绝" in caplog.text
    assert "1001" in caplog.text
    assert "alice" in caplog.text


async def test_run_expiration_check_shows_confirmation(monkeypatch):
    callback = Callback("admin:run_expiration", from_user_id=1001)
    service = FakeExpirationCheckService(
        ExpirationProcessResult(
            active_renewed=[_expiration_user(1, "active")],
            points_renewed=[_expiration_user(2, "points")],
            disabled=[_expiration_user(3, "disabled")],
            deleted=[_expiration_user(4, "deleted")],
        )
    )
    replaced = []

    async def fake_replace_panel(callback, text, *, reply_markup=None, panel_photo_path=None):
        replaced.append((text, reply_markup, panel_photo_path))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await handlers.run_expiration_check(
        callback,
        service,
        FakeSettings(admin_ids={1001}),
        FakeScheduler(),
    )

    assert callback.message.answers == []
    assert replaced
    assert "确认执行到期检查" in replaced[0][0]


async def test_run_activity_check_shows_confirmation(monkeypatch):
    callback = Callback("admin:run_activity", from_user_id=1001)
    service = FakeActivityCheckService(
        ActivityCheckResult(
            total_synced=5,
            active_renewed=[ActivityUserResult(1, "abs-1", "active")],
            disabled=[],
            deleted=[],
        )
    )
    replaced = []

    async def fake_replace_panel(callback, text, *, reply_markup=None, panel_photo_path=None):
        replaced.append((text, reply_markup, panel_photo_path))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await handlers.run_activity_check(
        callback,
        service,
        FakeSettings(admin_ids={1001}),
        FakeScheduler(),
    )

    assert callback.message.answers == []
    assert replaced
    assert "确认执行活跃续期" in replaced[0][0]


async def test_run_confirmed_active_renewal_sends_completion_message(monkeypatch):
    callback = Callback("admin:run_confirmed:active", from_user_id=1001)
    service = FakeActivityCheckService(
        ActivityCheckResult(
            total_synced=5,
            active_renewed=[ActivityUserResult(1, "abs-1", "active")],
            disabled=[],
            deleted=[],
        )
    )
    replaced = []

    async def fake_replace_panel(callback, text, *, reply_markup=None, panel_photo_path=None):
        replaced.append((text, reply_markup, panel_photo_path))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await handlers.run_confirmed_task(
        callback,
        service,
        FakeSettings(admin_ids={1001}),
        FakeScheduler(),
    )

    assert callback.message.answers == [("✅ 活跃续期已完成，共检测 5 位用户，活跃续期 1 位。", {})]
    assert replaced


async def test_clear_do_handler_refreshes_users_without_mutating_callback_data(monkeypatch):
    callback = FrozenDataCallback("admin:clear_do:all_users", from_user_id=1001)
    service = FakeClearUsersService()
    replaced = []

    async def fake_replace_panel(callback, text, *, reply_markup=None, panel_photo_path=None):
        replaced.append((text, reply_markup, panel_photo_path))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await admin_handlers.clear_do_handler(callback, service, FakeSettings(admin_ids={1001}))

    assert service.clear_calls == 1
    assert service.list_calls == [(0, 10, {})]
    assert callback.message.answers == [
        ("✅ 【清空所有用户】完成，共删除 2 个 ABS 账号，清除 3 条数据库记录。", {})
    ]
    assert replaced


async def test_run_confirmed_points_renewal_sends_completion_message(monkeypatch):
    callback = Callback("admin:run_confirmed:points", from_user_id=1001)
    service = FakeExpirationCheckService(
        ExpirationProcessResult(
            active_renewed=[],
            points_renewed=[_expiration_user(2, "points")],
            disabled=[],
            deleted=[],
        )
    )
    replaced = []

    async def fake_replace_panel(callback, text, *, reply_markup=None, panel_photo_path=None):
        replaced.append((text, reply_markup, panel_photo_path))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await handlers.run_confirmed_task(
        callback,
        service,
        FakeSettings(admin_ids={1001}),
        FakeScheduler(),
    )

    assert callback.message.answers == [("✅ 积分续期已完成，积分续期 1 位。", {})]
    assert replaced


async def test_run_confirmed_expiration_enforcement_sends_completion_message(monkeypatch):
    callback = Callback("admin:run_confirmed:expiration", from_user_id=1001)
    service = FakeExpirationCheckService(
        ExpirationProcessResult(
            active_renewed=[],
            points_renewed=[],
            disabled=[_expiration_user(3, "disabled")],
            deleted=[_expiration_user(4, "deleted")],
        )
    )
    replaced = []

    async def fake_replace_panel(callback, text, *, reply_markup=None, panel_photo_path=None):
        replaced.append((text, reply_markup, panel_photo_path))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await handlers.run_confirmed_task(
        callback,
        service,
        FakeSettings(admin_ids={1001}),
        FakeScheduler(),
    )

    assert callback.message.answers == [("✅ 到期检查已完成，禁用 1 位，删除 1 位。", {})]
    assert replaced


def test_created_codes_messages_are_split_before_telegram_text_limit():
    codes = [f"REG-{index:04d}" for index in range(1, 501)]

    messages = format_created_codes_messages(codes, limit=200)

    assert len(messages) > 1
    assert all(len(message) <= 200 for message in messages)
    assert "<code>REG-0001</code>" in messages[0]
    assert "<code>REG-0500</code>" in messages[-1]


def test_whitelist_code_payload_uses_count_without_days():
    code, days, count = parse_code_payload("3", RedeemCodeType.WHITELIST)

    assert code is None
    assert days is None
    assert count == 3


def test_registration_code_payload_still_requires_days():
    try:
        parse_code_payload("3", RedeemCodeType.REGISTRATION)
    except ValueError as exc:
        assert "days count" in str(exc)
    else:
        raise AssertionError("registration payload without days should fail")


def test_parse_credentials_preserves_password_spaces():
    assert parse_credentials("alice pass word") == ("alice", "pass word")


async def test_edit_prompt_message_edits_photo_caption():
    message = PromptMessage(has_photo=True)

    await _edit_prompt_message(message, "prompt")

    assert message.caption_edits == [("prompt", None)]
    assert message.text_edits == []


async def test_edit_prompt_message_edits_text_message():
    message = PromptMessage()

    await _edit_prompt_message(message, "prompt")

    assert message.text_edits == [("prompt", None)]
    assert message.caption_edits == []


async def test_target_grant_group_panel_shows_registration_claim_button(monkeypatch):
    service = FakeTargetService()
    state = FakeState()
    settings = FakeSettings(admin_ids={1})
    callback = Callback("target:1001:grant")
    callback.message.chat = Chat("supergroup")
    replaced = []

    async def fake_replace_panel(callback, text, *, reply_markup=None, panel_photo_path=None):
        replaced.append((text, reply_markup, panel_photo_path))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await target_actions(callback, state, service, settings)

    assert service.grants == [(1001, 1, None)]
    assert state.states == []
    assert state.data == []
    assert callback.answers == [("注册资格已发放", {})]
    assert replaced
    text, keyboard, panel_photo_path = replaced[0]
    assert '<a href="tg://user?id=1001">@1001</a> 的注册资格已发放。' in text
    assert panel_photo_path is None
    assert (
        _button_urls(keyboard)["🎁 领取注册资格"]
        == "https://t.me/AudiobookshelfBot?start=gift_1001"
    )
    assert callback.bot.sent_photos
    chat_id, _photo, private_text, private_keyboard, private_parse_mode = callback.bot.sent_photos[
        0
    ]
    assert chat_id == 1001
    assert private_text == handlers._registration_claim_text()
    assert private_parse_mode == "HTML"
    assert (
        _button_urls(private_keyboard)["🎁 领取注册资格"]
        == "https://t.me/AudiobookshelfBot?start=gift_1001"
    )


async def test_target_grant_private_panel_uses_photo_without_claim_button(monkeypatch):
    service = FakeTargetService(panel_photo_path="https://example.com/panel.jpg")
    state = FakeState()
    settings = FakeSettings(admin_ids={1})
    callback = Callback("target:1001:grant")
    callback.message.chat = Chat("private")
    replaced = []

    async def fake_replace_panel(callback, text, *, reply_markup=None, panel_photo_path=None):
        replaced.append((text, reply_markup, panel_photo_path))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await target_actions(callback, state, service, settings)

    assert callback.bot.sent_photos
    chat_id, _photo, private_text, private_keyboard, private_parse_mode = callback.bot.sent_photos[
        0
    ]
    assert chat_id == 1001
    assert private_text == handlers._registration_claim_text()
    assert (
        _button_urls(private_keyboard)["🎁 领取注册资格"]
        == "https://t.me/AudiobookshelfBot?start=gift_1001"
    )
    assert private_parse_mode == "HTML"
    text, keyboard, panel_photo_path = replaced[0]
    assert '<a href="tg://user?id=1001">@1001</a> 的注册资格已发放。' in text
    assert keyboard is None
    assert panel_photo_path == "https://example.com/panel.jpg"


async def test_target_grant_still_updates_panel_when_private_message_fails(monkeypatch):
    service = FakeTargetService()
    state = FakeState()
    settings = FakeSettings(admin_ids={1})
    callback = Callback("target:1001:grant")
    callback.bot = FakeBotUser(send_fails=True)
    replaced = []

    async def fake_replace_panel(callback, text, *, reply_markup=None, panel_photo_path=None):
        replaced.append((text, reply_markup, panel_photo_path))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await target_actions(callback, state, service, settings)

    assert service.grants == [(1001, 1, None)]
    assert callback.answers == [("注册资格已发放", {})]
    assert replaced


async def test_target_expiry_action_prompts_for_day_delta():
    service = FakeTargetService()
    state = FakeState()
    settings = FakeSettings(admin_ids={1})
    callback = Callback("target:1001:expiry")

    await target_actions(callback, state, service, settings)

    assert state.states == [handlers.AdminStates.expiry_delta]
    assert state.data == [{"target": 1001}]
    assert callback.answers == [(None, {})]
    assert callback.message.text_edits == [("请输入到期时间增减天数，例如：30 或 -7", None)]


async def test_target_reset_reports_missing_abs_user():
    class Service(FakeTargetService):
        async def reset_password(self, telegram_id):
            raise AudiobookshelfNotFoundError("用户不存在")

    callback = Callback("target:1001:reset")

    await target_actions(callback, FakeState(), Service(), FakeSettings(admin_ids={1}))

    assert callback.answers == [("重置失败：用户不存在", {"show_alert": True})]


async def test_target_whitelist_action_notifies_user():
    class Service(FakeTargetService):
        def __init__(self):
            super().__init__()
            self.whitelist_updates = []
            self.user_profile = TgUser(telegram_id=1001, is_whitelisted=False)

        async def get_profile(self, telegram_id):
            return self.user_profile

        async def set_whitelist(self, telegram_id, enabled):
            self.whitelist_updates.append((telegram_id, enabled))
            self.user_profile.is_whitelisted = enabled
            return self.user_profile

    callback = Callback("target:1001:white")
    service = Service()
    await target_actions(callback, FakeState(), service, FakeSettings(admin_ids={1}))

    assert service.whitelist_updates == [(1001, True)]
    assert callback.answers == [("已更新白名单", {})]
    assert len(callback.bot.sent_messages) == 1
    chat_id, text, _, _ = callback.bot.sent_messages[0]
    assert chat_id == 1001
    assert "白名单权益通知" in text
    # Also assert that it's one of the famous quotes
    quotes = [
        "欲知后事如何，且听下回分解",
        "说时迟，那时快",
        "书接上回",
        "花开两朵，各表一枝",
        "得来全不费工夫",
        "非是臣子多饶舌",
        "大江东去，浪淘尽",
        "天下风云出我辈",
    ]
    assert any(q in text for q in quotes)


async def test_adjust_expiry_from_payload_updates_and_returns_panel(monkeypatch):
    service = FakeTargetService()
    state = FakeState()
    await state.update_data(target=1001)
    message = AdminMessage("30", from_user_id=1)
    sent = []

    async def fake_send_panel(message_arg, caption, *, reply_markup=None, panel_photo_path=None):
        sent.append((message_arg, caption, reply_markup, panel_photo_path))

    monkeypatch.setattr(handlers, "send_panel", fake_send_panel)

    await adjust_expiry_from_payload(message, state, service, FakeSettings(admin_ids={1}))

    assert service.expiry_adjustments == [(1001, 30)]
    assert state.cleared is True
    assert sent
    assert "用户管理" in sent[0][1]


async def test_adjust_expiry_from_payload_rejects_non_integer():
    service = FakeTargetService()
    state = FakeState()
    await state.update_data(target=1001)
    message = AdminMessage("tomorrow", from_user_id=1)

    await adjust_expiry_from_payload(message, state, service, FakeSettings(admin_ids={1}))

    assert message.answers == [("请输入数字。", {})]
    assert service.expiry_adjustments == []
    assert state.cleared is False


async def test_start_gift_rejects_other_users(monkeypatch):
    class Service:
        async def is_initialized(self):
            return True

    async def fail_send_user_panel(*args, **kwargs):
        raise AssertionError("gift link should not open user panel")

    monkeypatch.setattr(handlers, "_send_user_panel", fail_send_user_panel)
    message = AdminMessage("/start gift_1001", from_user_id=2002)
    state = ClearState()

    await handlers.start_entry(message, state, Service(), FakeSettings(admin_ids=set()))

    assert message.answers == [("抱歉，这份礼物不属于您。", {})]


async def test_start_gift_enters_create_flow_for_recipient(monkeypatch):
    class Service:
        async def is_initialized(self):
            return True

    class State(FakeState):
        async def clear(self):
            pass

    sent_panels = []

    async def fake_send_panel(message, caption, *, reply_markup=None, panel_photo_path=None):
        sent_panels.append((message, caption, reply_markup, panel_photo_path))

    async def fail_send_user_panel(*args, **kwargs):
        raise AssertionError("gift link should enter create flow")

    monkeypatch.setattr(handlers, "_send_user_panel", fail_send_user_panel)
    monkeypatch.setattr(handlers, "send_panel", fake_send_panel)
    message = AdminMessage("/start gift_1001", from_user_id=1001)
    state = State()

    await handlers.start_entry(message, state, Service(), FakeSettings(admin_ids=set()))

    assert state.states == [handlers.UserStates.create_username]
    assert message.answers == []
    assert sent_panels == [(message, handlers._registration_username_prompt(), None, None)]


async def test_start_register_enters_create_account_flow(monkeypatch):
    class Service:
        async def is_initialized(self):
            return True

    class State(FakeState):
        def __init__(self):
            super().__init__()
            self.cleared = False

        async def clear(self):
            self.cleared = True

    sent_panels = []

    async def fake_send_panel(message, caption, *, reply_markup=None, panel_photo_path=None):
        sent_panels.append((message, caption, reply_markup, panel_photo_path))

    async def fail_send_user_panel(*args, **kwargs):
        raise AssertionError("register deep link should enter create flow")

    monkeypatch.setattr(handlers, "_send_user_panel", fail_send_user_panel)
    monkeypatch.setattr(handlers, "send_panel", fake_send_panel)
    message = AdminMessage("/start register", from_user_id=1001)
    state = State()

    await handlers.start_entry(message, state, Service(), FakeSettings(admin_ids=set()))

    assert state.cleared is True
    assert state.states == [handlers.UserStates.create_username]
    assert message.answers == []
    assert sent_panels == [(message, handlers._registration_username_prompt(), None, None)]


async def test_replace_user_panel_syncs_abs_activity_before_rendering(monkeypatch):
    class Service:
        def __init__(self):
            self.synced = []

        async def get_public_settings(self):
            return _settings()

        async def sync_profile_activity(self, telegram_id):
            self.synced.append(telegram_id)
            return TgUser(telegram_id=telegram_id, abs_user_id="usr_1", abs_username="alice")

        async def get_system_settings(self):
            return type("SystemSettings", (), {"panel_photo_path": None})()

    replaced = []

    async def fake_replace_panel(callback, text, *, reply_markup=None, panel_photo_path=None):
        replaced.append((text, reply_markup, panel_photo_path))

    service = Service()
    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await handlers._replace_user_panel(
        Callback("me:info"), service, FakeSettings(admin_ids=set()), 1005
    )

    assert service.synced == [1005]
    assert replaced
    assert "ABS 账号：alice" in replaced[0][0]


async def test_ask_server_lines_shows_current_html_lines():
    class Service:
        async def get_public_settings(self):
            return _settings_with_lines("<b>线路</b>\nhttps://example.com")

    callback = Callback("admin:lines", from_user_id=1001)
    state = FakeState()

    await handlers.ask_server_lines(callback, state, Service(), FakeSettings(admin_ids={1001}))

    assert state.states == [handlers.AdminStates.server_lines]
    assert callback.message.text_edits
    prompt = callback.message.text_edits[0][0]
    assert "支持 HTML 格式" in prompt
    assert "当前内容（原始文本）" in prompt
    assert "&lt;b&gt;线路&lt;/b&gt;" in prompt
    assert "MarkdownV2" not in prompt


async def test_user_lines_panel_uses_html_parse_mode(monkeypatch):
    class Service:
        async def get_public_settings(self):
            return _settings_with_lines('<b>线路</b>\n<a href="https://example.com">入口</a>')

        async def get_system_settings(self):
            return type("SystemSettings", (), {"panel_photo_path": None})()

        async def check_server_reachable(self):
            return True

        async def get_total_book_count(self):
            return None

    replaced = []

    async def fake_replace_panel(
        callback, text, *, reply_markup=None, panel_photo_path=None, parse_mode=None
    ):
        replaced.append((text, reply_markup, panel_photo_path, parse_mode))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await handlers.user_actions(
        Callback("me:lines", from_user_id=1001),
        FakeState(),
        Service(),
        FakeSettings(admin_ids=set()),
    )

    assert replaced
    assert replaced[0][0].startswith("<b>线路</b>")
    assert replaced[0][3] == handlers.ParseMode.HTML


async def test_user_lines_panel_shows_book_count_when_online(monkeypatch):
    class Service:
        async def get_public_settings(self):
            return _settings_with_lines("线路信息")

        async def get_system_settings(self):
            return type("SystemSettings", (), {"panel_photo_path": None})()

        async def check_server_reachable(self):
            return True

        async def get_total_book_count(self):
            return 180

    replaced = []

    async def fake_replace_panel(
        callback, text, *, reply_markup=None, panel_photo_path=None, parse_mode=None
    ):
        replaced.append((text, reply_markup, panel_photo_path, parse_mode))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await handlers.user_actions(
        Callback("me:lines", from_user_id=1001),
        FakeState(),
        Service(),
        FakeSettings(admin_ids=set()),
    )

    assert replaced
    assert "180" in replaced[0][0]


async def test_user_lines_panel_omits_book_count_when_none(monkeypatch):
    class Service:
        async def get_public_settings(self):
            return _settings_with_lines("线路信息")

        async def get_system_settings(self):
            return type("SystemSettings", (), {"panel_photo_path": None})()

        async def check_server_reachable(self):
            return True

        async def get_total_book_count(self):
            return None

    replaced = []

    async def fake_replace_panel(
        callback, text, *, reply_markup=None, panel_photo_path=None, parse_mode=None
    ):
        replaced.append((text, reply_markup, panel_photo_path, parse_mode))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await handlers.user_actions(
        Callback("me:lines", from_user_id=1001),
        FakeState(),
        Service(),
        FakeSettings(admin_ids=set()),
    )

    assert replaced
    assert "📚" not in replaced[0][0]


async def test_user_lines_panel_offline_no_book_count(monkeypatch):
    book_count_called = []

    class Service:
        async def get_public_settings(self):
            return _settings_with_lines("线路信息")

        async def get_system_settings(self):
            return type("SystemSettings", (), {"panel_photo_path": None})()

        async def check_server_reachable(self):
            return False

        async def get_total_book_count(self):
            book_count_called.append(True)
            return 180

    replaced = []

    async def fake_replace_panel(
        callback, text, *, reply_markup=None, panel_photo_path=None, parse_mode=None
    ):
        replaced.append((text, reply_markup, panel_photo_path, parse_mode))

    monkeypatch.setattr("absbot.handlers.replace_panel", fake_replace_panel)

    await handlers.user_actions(
        Callback("me:lines", from_user_id=1001),
        FakeState(),
        Service(),
        FakeSettings(admin_ids=set()),
    )

    assert replaced
    assert "📚" not in replaced[0][0]
    assert not book_count_called


async def test_user_checkin_closed_shows_alert_and_refreshes_panel(monkeypatch):
    class Service:
        async def get_public_settings(self):
            return _settings(checkin_enabled=False)

        async def checkin(self, telegram_id, *, today=None):
            raise ValueError("签到未开放")

    refreshed = []

    async def fake_replace_user_panel(callback, service, settings, telegram_id):
        refreshed.append((callback.data, telegram_id))

    monkeypatch.setattr("absbot.handlers._replace_user_panel", fake_replace_user_panel)

    callback = Callback("me:checkin", from_user_id=1001)

    await handlers.user_actions(
        callback,
        FakeState(),
        Service(),
        FakeSettings(admin_ids=set()),
    )

    assert callback.answers == [("签到未开放", {"show_alert": True})]
    assert refreshed == [("me:checkin", 1001)]


async def test_user_request_rebind_reports_unexpected_review_record_failure():
    class Service:
        async def get_system_settings(self):
            return type("SystemSettings", (), {"rebind_review_chat_id": -100123})()

        async def create_rebind_request(self, telegram_id, username, password):
            return handlers.RebindRequestSnapshot(
                id=12,
                requester_telegram_id=telegram_id,
                abs_user_id="usr_1",
                abs_username=username,
                current_telegram_id=7001,
                status=handlers.RebindRequestStatus.PENDING,
                review_chat_id=None,
                review_message_id=None,
                reviewed_by=None,
                reviewed_at=None,
                created_at=None,
            )

        async def set_rebind_review_message(self, request_id, *, chat_id, message_id):
            raise RuntimeError("database unavailable")

    message = RebindMessage("alice secret", from_user_id=7002)
    state = ClearState()

    await handlers.user_request_rebind(message, state, Service(), FakeSettings(admin_ids=set()))

    assert message.deleted is True
    assert state.cleared is True
    assert message.answers == [("提交换绑申请失败，请联系管理员。", {})]


async def test_user_request_rebind_reports_unexpected_review_send_failure():
    class Bot:
        async def send_message(self, chat_id, text, *, reply_markup=None):
            raise RuntimeError("network stuck")

    class Service:
        async def get_system_settings(self):
            return type("SystemSettings", (), {"rebind_review_chat_id": -100123})()

        async def create_rebind_request(self, telegram_id, username, password):
            return handlers.RebindRequestSnapshot(
                id=12,
                requester_telegram_id=telegram_id,
                abs_user_id="usr_1",
                abs_username=username,
                current_telegram_id=7001,
                status=handlers.RebindRequestStatus.PENDING,
                review_chat_id=None,
                review_message_id=None,
                reviewed_by=None,
                reviewed_at=None,
                created_at=None,
            )

        async def set_rebind_review_message(self, request_id, *, chat_id, message_id):
            raise AssertionError("review message should not be recorded")

    message = RebindMessage("alice secret", from_user_id=7002)
    message.bot = Bot()
    state = ClearState()

    await handlers.user_request_rebind(message, state, Service(), FakeSettings(admin_ids=set()))

    assert message.deleted is True
    assert state.cleared is True
    assert message.answers == [("发送换绑审核消息失败，请联系管理员：network stuck", {})]


async def test_user_request_rebind_times_out_review_message_send(monkeypatch):
    class Bot:
        async def send_message(self, chat_id, text, *, reply_markup=None):
            await asyncio.sleep(1)

    class Service:
        async def get_system_settings(self):
            return type("SystemSettings", (), {"rebind_review_chat_id": -100123})()

        async def create_rebind_request(self, telegram_id, username, password):
            return handlers.RebindRequestSnapshot(
                id=12,
                requester_telegram_id=telegram_id,
                abs_user_id="usr_1",
                abs_username=username,
                current_telegram_id=7001,
                status=handlers.RebindRequestStatus.PENDING,
                review_chat_id=None,
                review_message_id=None,
                reviewed_by=None,
                reviewed_at=None,
                created_at=None,
            )

        async def set_rebind_review_message(self, request_id, *, chat_id, message_id):
            raise AssertionError("review message should not be recorded")

    monkeypatch.setattr(handlers, "REBIND_REVIEW_SEND_TIMEOUT_SECONDS", 0.01, raising=False)
    message = RebindMessage("alice secret", from_user_id=7002)
    message.bot = Bot()
    state = ClearState()

    await handlers.user_request_rebind(message, state, Service(), FakeSettings(admin_ids=set()))

    assert message.deleted is True
    assert state.cleared is True
    assert message.answers == [("发送换绑审核消息超时，请联系管理员。", {})]


def _settings(
    checkin_enabled: bool = True,
    *,
    registration_open: bool = False,
    registration_slots: int = 0,
) -> PublicSettings:
    return PublicSettings(
        registration_open=registration_open,
        registration_slots=registration_slots,
        server_lines="lines",
        checkin_enabled=checkin_enabled,
        checkin_min_points=1,
        checkin_max_points=10,
        active_retention_enabled=True,
        active_retention_window_days=30,
        active_retention_extension_days=30,
        points_renewal_enabled=True,
        points_renewal_cost_points=100,
        points_renewal_extension_days=30,
        expiration_enforcement_enabled=True,
        points_unban_enabled=False,
        points_unban_cost_points=100,
        daily_leaderboard_enabled=True,
        weekly_leaderboard_enabled=True,
    )


def _settings_with_lines(server_lines: str) -> PublicSettings:
    settings = _settings()
    return PublicSettings(
        **{
            f.name: getattr(settings, f.name)
            for f in settings.__dataclass_fields__.values()
            if f.name != "server_lines"
        },
        server_lines=server_lines,
    )


def _button_texts(keyboard):
    return [button.text for row in keyboard.inline_keyboard for button in row]


def _button_callbacks(keyboard):
    return {button.text: button.callback_data for row in keyboard.inline_keyboard for button in row}


def _button_urls(keyboard):
    return {button.text: button.url for row in keyboard.inline_keyboard for button in row}


class FakeState:
    def __init__(self):
        self.states = []
        self.data = []
        self.cleared = False

    async def set_state(self, state):
        self.states.append(state)

    async def update_data(self, **data):
        self.data.append(data)

    async def get_data(self):
        merged = {}
        for item in self.data:
            merged.update(item)
        return merged

    async def clear(self):
        self.cleared = True


class FakeSettings:
    def __init__(self, *, admin_ids):
        self.admin_ids = set(admin_ids)
        self.owner_tg_id = None

    def is_admin(self, telegram_id):
        return telegram_id in self.admin_ids

    def is_owner(self, telegram_id):
        return False


class FakeTargetService:
    def __init__(self, *, panel_photo_path=None):
        self.grants = []
        self.expiry_adjustments = []
        self.panel_photo_path = panel_photo_path

    async def grant_registration(self, telegram_id, *, credits, days=None):
        self.grants.append((telegram_id, credits, days))
        return TgUser(telegram_id=telegram_id, registration_credits=credits)

    async def admin_adjust_expiry(self, telegram_id, *, delta):
        self.expiry_adjustments.append((telegram_id, delta))
        return TgUser(
            telegram_id=telegram_id,
            abs_user_id="usr_1",
            abs_username="alice",
        )

    async def get_profile(self, telegram_id):
        return TgUser(telegram_id=telegram_id, registration_credits=1)

    async def get_system_settings(self):
        return type("SystemSettings", (), {"panel_photo_path": self.panel_photo_path})()


def test_user_panel_shows_unban_button_when_disabled_and_feature_enabled():
    profile = TgUser(telegram_id=1, is_disabled=True, abs_user_id="usr_1")
    settings = _settings()
    settings_with_unban = PublicSettings(
        **{
            f.name: getattr(settings, f.name)
            for f in settings.__dataclass_fields__.values()
            if f.name not in ("points_unban_enabled", "points_unban_cost_points")
        },
        points_unban_enabled=True,
        points_unban_cost_points=80,
    )
    kb = user_panel_keyboard(profile=profile, settings=settings_with_unban)
    callbacks = _button_callbacks(kb)
    assert "me:unban_request" in callbacks.values()
    assert any("80" in text for text in callbacks)


def test_user_panel_hides_unban_button_when_not_disabled():
    profile = TgUser(telegram_id=1, is_disabled=False, abs_user_id="usr_1")
    settings = _settings()
    settings_with_unban = PublicSettings(
        **{
            f.name: getattr(settings, f.name)
            for f in settings.__dataclass_fields__.values()
            if f.name not in ("points_unban_enabled", "points_unban_cost_points")
        },
        points_unban_enabled=True,
        points_unban_cost_points=80,
    )
    kb = user_panel_keyboard(profile=profile, settings=settings_with_unban)
    callbacks = _button_callbacks(kb)
    assert "me:unban_request" not in callbacks.values()


# ── Backup panel tests ──────────────────────────────────────────────────────


async def test_backup_panel_shows_local_backups(tmp_path):
    """backup_panel handler 能列出本地备份文件。"""
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock

    from absbot.config import Settings
    from absbot.handlers import backup_panel

    backup_dir = str(tmp_path / "backups")
    Path(backup_dir).mkdir(parents=True)
    (Path(backup_dir) / "backup_20260522_050000.sql").write_text("-- test")

    callback = MagicMock()
    callback.from_user.id = 777
    callback.data = "admin:backup"
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.caption = None

    settings = Settings(
        bot_token="tok",
        admin_tg_ids={777},
        mysql_dsn="sqlite://",
        abs_base_url="https://x.com",
        abs_api_token="t",
        owner_tg_id=777,
        backup_dir=backup_dir,
        backup_keep_count=7,
    )

    await backup_panel(callback, settings)

    callback.answer.assert_called_once()
    call_kwargs = callback.message.edit_text.call_args
    assert "备份管理" in str(call_kwargs)
    assert "admin:backup:restore:backup_20260522_050000.sql" in str(call_kwargs)


async def test_backup_panel_rejects_non_owner(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from absbot.config import Settings
    from absbot.handlers import backup_panel

    callback = MagicMock()
    callback.from_user.id = 778
    callback.answer = AsyncMock()

    settings = Settings(
        bot_token="tok",
        admin_tg_ids={778},
        mysql_dsn="sqlite://",
        abs_base_url="https://x.com",
        abs_api_token="t",
        owner_tg_id=777,
        backup_dir=str(tmp_path / "backups"),
        backup_keep_count=7,
    )

    await backup_panel(callback, settings)

    callback.answer.assert_called_once_with("没有权限", show_alert=True)


def test_user_panel_hides_unban_button_when_feature_disabled():
    profile = TgUser(telegram_id=1, is_disabled=True, abs_user_id="usr_1")
    settings = _settings()  # points_unban_enabled=False
    kb = user_panel_keyboard(profile=profile, settings=settings)
    callbacks = _button_callbacks(kb)
    assert "me:unban_request" not in callbacks.values()
