from __future__ import annotations

import sys

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from absbot.models import RebindRequestStatus
from absbot.panels import replace_panel, send_panel
from absbot.scheduler import sync_registration_announcement
from absbot.service import RebindRequestSnapshot

from .middleware import MainGroupMembershipMiddleware
from .states import SetupStates, UserStates, AdminStates
from .helpers import (
    REBIND_REVIEW_SEND_TIMEOUT_SECONDS,
    _admin_panel_text,
    _edit_prompt_message,
    _registration_claim_text,
    _registration_username_prompt,
    _replace_user_panel,
    _send_user_panel,
    _setup_summary_text,
    _target_user_text,
    _user_panel_text,
    format_created_codes_messages,
    is_allowed_chat_member_status,
    is_private_start,
    is_setup_event,
    is_setup_update,
    parse_code_payload,
    parse_credentials,
    parse_pp_target,
    should_show_setup_notice,
)
from .admin import (
    adjust_expiry_from_payload,
    ask_server_lines,
    run_expiration_check,
    run_activity_check,
    run_confirmed_task,
    set_registration_slots,
    target_actions,
)
from .user import (
    cleanup_group_start_command,
    pp_entry,
    start_entry,
    user_actions,
    user_create_account,
    user_request_rebind,
)
from .setup import _SETUP_PROMPTS, _setup_finish, setup_set_disabled_delete
from .backup import backup_panel
from . import admin, backup, setup, user

router = Router()

router.message.outer_middleware(MainGroupMembershipMiddleware())
router.callback_query.outer_middleware(MainGroupMembershipMiddleware())

router.include_router(user.router)
router.include_router(admin.router)
router.include_router(setup.router)
router.include_router(backup.router)

_SUBMODULE_NAMES = (
    "absbot.handlers.helpers",
    "absbot.handlers.user",
    "absbot.handlers.admin",
    "absbot.handlers.setup",
    "absbot.handlers.backup",
)


class _HandlersModule(sys.modules[__name__].__class__):
    """Module subclass that propagates attribute sets to sub-modules.

    This ensures test monkeypatching via ``absbot.handlers.xxx = mock``
    works even after the original single-file module was split into a package.
    """

    def __setattr__(self, name: str, value: object) -> None:
        for mod_name in _SUBMODULE_NAMES:
            mod = sys.modules.get(mod_name)
            if mod is not None and name in mod.__dict__:
                mod.__dict__[name] = value
        self.__dict__[name] = value


sys.modules[__name__].__class__ = _HandlersModule


__all__ = [
    "router",
    "SetupStates",
    "UserStates",
    "AdminStates",
    "MainGroupMembershipMiddleware",
    "RebindRequestSnapshot",
    "RebindRequestStatus",
    "TelegramAPIError",
    "TelegramBadRequest",
    "ParseMode",
    "REBIND_REVIEW_SEND_TIMEOUT_SECONDS",
    "_admin_panel_text",
    "_edit_prompt_message",
    "_SETUP_PROMPTS",
    "_registration_claim_text",
    "_registration_username_prompt",
    "_replace_user_panel",
    "_send_user_panel",
    "_setup_finish",
    "_setup_summary_text",
    "_target_user_text",
    "_user_panel_text",
    "format_created_codes_messages",
    "is_allowed_chat_member_status",
    "is_private_start",
    "is_setup_event",
    "is_setup_update",
    "parse_code_payload",
    "parse_credentials",
    "parse_pp_target",
    "replace_panel",
    "send_panel",
    "should_show_setup_notice",
    "sync_registration_announcement",
    "adjust_expiry_from_payload",
    "ask_server_lines",
    "cleanup_group_start_command",
    "pp_entry",
    "run_expiration_check",
    "run_activity_check",
    "run_confirmed_task",
    "set_registration_slots",
    "setup_set_disabled_delete",
    "start_entry",
    "target_actions",
    "user_actions",
    "user_create_account",
    "user_request_rebind",
    "backup_panel",
]
