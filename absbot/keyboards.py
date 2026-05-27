from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from absbot.models import RedeemCode, TgUser
from absbot.service import PublicSettings


def admin_panel_keyboard(
    *,
    owner_id: int,
    include_start_back: bool = False,
    is_owner: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📝 开放注册", callback_data="admin:reg"),
        InlineKeyboardButton(text="📡 设置线路", callback_data="admin:lines"),
    )
    builder.row(
        InlineKeyboardButton(text="🎟️ 兑换码", callback_data="admin:codes"),
        InlineKeyboardButton(text="👥 用户列表", callback_data="admin:users:0"),
        InlineKeyboardButton(text="⭐ 白名单列表", callback_data="admin:white:0"),
    )
    builder.row(
        InlineKeyboardButton(text="⚙️ 任务控制", callback_data="admin:tasks"),
        InlineKeyboardButton(text="🔔 签到与解禁设置", callback_data="admin:checkin_unban")
    )

    if is_owner:
        builder.row(
            InlineKeyboardButton(text="🗄 备份管理", callback_data="admin:backup"),
            InlineKeyboardButton(text="🚀 初始化向导", callback_data="admin:setup"),
        )
    if include_start_back:
        builder.row(InlineKeyboardButton(text="⬅️ 返回主面板", callback_data="admin:start"))
    return builder.as_markup()


def checkin_unban_panel_keyboard(
    *,
    checkin_enabled: bool = False,
    points_unban_enabled: bool = False,
    points_unban_cost_points: int = 100,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"🎁 签到开关：{'开' if checkin_enabled else '关'}",
            callback_data="admin:checkin",
        )
    )
    if checkin_enabled:
        builder.row(
            InlineKeyboardButton(text="🎲 设置签到积分", callback_data="admin:checkinpoints")
        )
    builder.row(
        InlineKeyboardButton(
            text=f"🔓 积分解禁：{'开' if points_unban_enabled else '关'}",
            callback_data="admin:toggle_unban",
        )
    )
    if points_unban_enabled:
        builder.row(
            InlineKeyboardButton(
                text=f"💰 解禁费用：{points_unban_cost_points} 积分 [修改]",
                callback_data="admin:set_unban_cost",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ 返回管理面板", callback_data="admin:home"))
    return builder.as_markup()


def tasks_panel_keyboard(*, active_enabled: bool, points_enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"🕒 活跃检测：{'开' if active_enabled else '关'}",
            callback_data="admin:active",
        ),
        InlineKeyboardButton(
            text=f"💎 积分续期：{'开' if points_enabled else '关'}",
            callback_data="admin:pointsrenew",
        ),
    )
    builder.row(
        InlineKeyboardButton(text="🔍 执行活跃检测", callback_data="admin:run_activity"),
        InlineKeyboardButton(text="⏰ 执行到期检测", callback_data="admin:run_expiration"),
    )
    builder.row(
        InlineKeyboardButton(text="📊 发布每日榜", callback_data="admin:push_leaderboard:daily"),
        InlineKeyboardButton(text="📊 发布每周榜", callback_data="admin:push_leaderboard:weekly"),
    )
    builder.row(InlineKeyboardButton(text="⬅️ 返回管理面板", callback_data="admin:home"))
    return builder.as_markup()


def setup_step_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⏭️ 跳过", callback_data="setup:skip"),
        InlineKeyboardButton(text="🚫 中止向导", callback_data="setup:cancel"),
    )
    return builder.as_markup()


def code_panel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🆕 创建注册码", callback_data="admin:mkcode:registration"),
        InlineKeyboardButton(text="🔁 创建续期码", callback_data="admin:mkcode:renewal"),
    )
    builder.row(
        InlineKeyboardButton(text="⭐ 创建白名单码", callback_data="admin:mkcode:whitelist"),
    )
    builder.row(
        InlineKeyboardButton(text="📋 查看注册码", callback_data="admin:codelist:registration:0"),
        InlineKeyboardButton(text="📋 查看续期码", callback_data="admin:codelist:renewal:0"),
        InlineKeyboardButton(text="📋 查看白名单码", callback_data="admin:codelist:whitelist:0"),
    )
    builder.row(InlineKeyboardButton(text="⬅️ 返回管理面板", callback_data="admin:home"))
    return builder.as_markup()


def user_panel_keyboard(
    *,
    profile: TgUser,
    settings: PublicSettings,
    is_admin: bool = False,
    include_close: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    info_buttons = []
    if profile.abs_user_id:
        info_buttons.append(InlineKeyboardButton(text="📡 查看线路", callback_data="me:lines"))
    if info_buttons:
        builder.row(*info_buttons)
    if profile.abs_user_id:
        builder.row(InlineKeyboardButton(text="🎟️ 兑换码", callback_data="me:redeem"))
    else:
        builder.row(
            InlineKeyboardButton(text="🆕 创建账号", callback_data="me:create"),
            InlineKeyboardButton(text="🎟️ 兑换码", callback_data="me:redeem"),
        )
    if profile.abs_user_id:
        builder.row(
            InlineKeyboardButton(text="🔐 重置密码", callback_data="me:reset"),
            InlineKeyboardButton(text="🗑️ 注销账号", callback_data="me:delete"),
        )
    else:
        builder.row(
            InlineKeyboardButton(text="🔗 绑定旧账", callback_data="me:bind"),
            InlineKeyboardButton(text="🔄 申请换绑", callback_data="me:rebind"),
        )
    if profile.is_disabled and settings.points_unban_enabled:
        builder.row(
            InlineKeyboardButton(
                text=f"🔓 积分自助解禁（需 {settings.points_unban_cost_points} 积分）",
                callback_data="me:unban_request",
            )
        )
    if settings.checkin_enabled:
        builder.row(InlineKeyboardButton(text="🎁 每日签到", callback_data="me:checkin"))
    if is_admin:
        builder.row(InlineKeyboardButton(text="⚙️ 管理面板", callback_data="admin:home"))
    if include_close:
        builder.row(InlineKeyboardButton(text="❌ 关闭", callback_data=f"close:{profile.telegram_id}"))
    return builder.as_markup()


def target_user_keyboard(user: TgUser, *, owner_id: int) -> InlineKeyboardMarkup:
    tid = user.telegram_id
    whitelist_text = "取消白名单" if user.is_whitelisted else "赠送白名单"
    builder = InlineKeyboardBuilder()
    if user.abs_user_id:
        builder.row(
            InlineKeyboardButton(text="🔐 重置密码", callback_data=f"target:{tid}:reset"),
            InlineKeyboardButton(text="🗑️ 删除用户", callback_data=f"target:{tid}:delete"),
        )
        builder.row(
            InlineKeyboardButton(text=f"⭐ {whitelist_text}", callback_data=f"target:{tid}:white"),
            InlineKeyboardButton(text="⏰ 调整到期时间", callback_data=f"target:{tid}:expiry"),
        )
    if not user.abs_user_id and (user.registration_credits or 0) <= 0:
        builder.row(
            InlineKeyboardButton(text="🎁 赠送注册资格", callback_data=f"target:{tid}:grant"),
        )
    builder.row(
        InlineKeyboardButton(text="💎 调整积分", callback_data=f"target:{tid}:points"),
        InlineKeyboardButton(text="❌ 关闭", callback_data=f"close:{owner_id}")
    )
    return builder.as_markup()


def users_page_keyboard(users: list[TgUser], *, page: int, kind: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user in users:
        title = user.abs_username or str(user.telegram_id)
        builder.row(
            InlineKeyboardButton(
                text=f"{title} ({user.telegram_id})",
                callback_data=f"admin:user:{user.telegram_id}",
            )
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ 上一页", callback_data=f"admin:{kind}:{page - 1}"))
    if len(users) == 10:
        nav.append(InlineKeyboardButton(text="➡️ 下一页", callback_data=f"admin:{kind}:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="⬅️ 返回管理面板", callback_data="admin:home"))
    return builder.as_markup()


def code_list_keyboard(
    codes: list[RedeemCode], *, code_type: str, page: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # for code in codes:
    #     builder.row(
    #         InlineKeyboardButton(
    #             text="🗑️ 删除", callback_data=f"code:del:{code_type}:{page}:{code.id}"
    #         ),
    #     )
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ 上一页", callback_data=f"admin:codelist:{code_type}:{page - 1}"
            )
        )
    if len(codes) == 10:
        nav.append(
            InlineKeyboardButton(
                text="➡️ 下一页", callback_data=f"admin:codelist:{code_type}:{page + 1}"
            )
        )
    if nav:
        builder.row(*nav)
    builder.row(
        InlineKeyboardButton(
            text="🗑️ 删除本页", callback_data=f"code:delpage:{code_type}:{page}"
        ),
        InlineKeyboardButton(
            text="🗑️ 删除所有", callback_data=f"code:delall:{code_type}"
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="✅ 删除已使用", callback_data=f"code:delused:{code_type}"
        ),
        InlineKeyboardButton(
            text="⭕ 删除未使用", callback_data=f"code:delunused:{code_type}"
        ),
    )
    builder.row(InlineKeyboardButton(text="⬅️ 返回管理面板", callback_data="admin:codes"))
    return builder.as_markup()


def registration_announcement_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
            InlineKeyboardButton(
                text="🚀 开始注册",
                url=f"https://t.me/{bot_username}?start=register",
            )
    )
    return builder.as_markup()


def registration_claim_keyboard(bot_username: str, telegram_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
            InlineKeyboardButton(
                text="🎁 领取注册资格",
                url=f"https://t.me/{bot_username}?start=gift_{telegram_id}",
            )
    )
    return builder.as_markup()


def lines_panel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ 返回个人中心", callback_data="me:info"))
    return builder.as_markup()


def confirm_delete_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ 确认注销", callback_data="me:delete_confirm"),
        InlineKeyboardButton(text="↩️ 取消", callback_data="me:info"),
    )
    return builder.as_markup()


def rebind_review_keyboard(request_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ 同意换绑", callback_data=f"rebind:approve:{request_id}"),
        InlineKeyboardButton(text="❌ 拒绝换绑", callback_data=f"rebind:reject:{request_id}"),
    )
    return builder.as_markup()


def unban_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ 确认解禁", callback_data="me:unban_confirm"),
        InlineKeyboardButton(text="❌ 取消", callback_data="me:info"),
    )
    return builder.as_markup()


def backup_panel_keyboard(backups: list[str], owner_id: int) -> InlineKeyboardMarkup:
    """备份管理面板键盘：列出本地备份（每个一行）+ 立即备份 + 返回。"""
    builder = InlineKeyboardBuilder()
    for filename in backups:
        builder.row(
            InlineKeyboardButton(
                text=f"📦 {filename.removeprefix('backup_').removesuffix('.sql')}",
                callback_data=f"admin:backup:restore:{filename}",
            )
        )
    builder.row(
        InlineKeyboardButton(text="⚡️ 立即备份", callback_data="admin:backup:run")
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ 返回管理面板", callback_data="admin:home")
    )
    return builder.as_markup()


def backup_confirm_keyboard(filename: str) -> InlineKeyboardMarkup:
    """恢复确认键盘：确认恢复 + 取消。"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ 确认恢复",
            callback_data=f"admin:backup:do_restore:{filename}",
        ),
        InlineKeyboardButton(text="✗ 取消", callback_data="admin:backup"),
    )
    return builder.as_markup()
