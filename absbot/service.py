from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Select, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from absbot.abs_client import AudiobookshelfError, AudiobookshelfNotFoundError
from absbot.models import (
    BotSetting,
    RebindRequest,
    RebindRequestStatus,
    RedeemCode,
    RedeemCodeType,
    RedeemCodeUse,
    RegistrationQueue,
    RegistrationQueueStatus,
    TgUser,
)
from absbot.security import generate_code, generate_password, normalize_code
from absbot.timeutils import ensure_utc, format_dt, from_millis, max_datetime, utc_now


logger = logging.getLogger(__name__)

MAX_REDEEM_CODES_PER_BATCH = 50
ACTIVE_REGISTRATION_QUEUE_STATUSES = (
    RegistrationQueueStatus.PENDING,
    RegistrationQueueStatus.PROCESSING,
)
_REGISTRATION_LOCK = asyncio.Lock()
_REGISTRATION_QUEUE_PROCESS_LOCK = asyncio.Lock()


class InsufficientPointsError(Exception):
    """用户积分不足以完成操作时抛出。"""

    def __init__(self, current: int, needed: int) -> None:
        self.current = current
        self.needed = needed
        self.deficit = needed - current
        super().__init__(f"积分不足：需要 {needed} 积分，当前 {current} 积分")


@dataclass(frozen=True)
class PublicSettings:
    registration_open: bool
    registration_slots: int
    server_lines: str
    checkin_enabled: bool
    checkin_min_points: int
    checkin_max_points: int
    active_retention_enabled: bool
    active_retention_window_days: int
    active_retention_extension_days: int
    points_renewal_enabled: bool
    points_renewal_cost_points: int
    points_renewal_extension_days: int
    points_unban_enabled: bool
    points_unban_cost_points: int


@dataclass(frozen=True)
class SystemSettings:
    default_register_days: int
    panel_photo_path: str | None
    rebind_review_chat_id: int | None
    main_group_chat_id: int | None
    main_group_link: str | None
    disabled_delete_after_days: int


@dataclass(frozen=True)
class AccountCreationResult:
    abs_user_id: str
    username: str
    initial_password: str
    expires_at: datetime | None


@dataclass(frozen=True)
class RegistrationQueueProcessResult:
    queue_id: int
    telegram_id: int
    success: bool
    username: str | None = None
    initial_password: str | None = None
    expires_at: datetime | None = None
    error_message: str | None = None
    registration_closed: bool = False


@dataclass(frozen=True)
class AccountBindingResult:
    abs_user_id: str
    username: str
    expires_at: datetime | None


@dataclass(frozen=True)
class PasswordResetResult:
    username: str
    password: str


@dataclass(frozen=True)
class CheckinResult:
    points: int
    awarded: int
    already_checked_in: bool


@dataclass(frozen=True)
class RedeemResult:
    message: str
    registration_credits: int
    expires_at: datetime | None
    is_whitelisted: bool
    points: int


@dataclass(frozen=True)
class ExpirationUserResult:
    telegram_id: int
    abs_user_id: str
    abs_username: str | None
    expires_at: datetime | None
    points_spent: int = 0
    disabled_at: datetime | None = None
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class ExpirationProcessResult:
    active_renewed: list[ExpirationUserResult]
    points_renewed: list[ExpirationUserResult]
    disabled: list[ExpirationUserResult]
    deleted: list[ExpirationUserResult]

    @property
    def has_changes(self) -> bool:
        return bool(self.active_renewed or self.points_renewed or self.disabled or self.deleted)


@dataclass(frozen=True)
class ActivityUserResult:
    telegram_id: int
    abs_user_id: str
    abs_username: str | None
    disabled_at: datetime | None = None
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class ActivityCheckResult:
    total_synced: int
    disabled: list[ActivityUserResult]
    deleted: list[ActivityUserResult]


@dataclass
class SyncResult:
    synced_count: int
    recreated: list[tuple[int, str, str]]  # (telegram_id, abs_username, new_password)
    failed_count: int


@dataclass(frozen=True)
class RebindRequestSnapshot:
    id: int
    requester_telegram_id: int
    abs_user_id: str
    abs_username: str
    current_telegram_id: int | None
    status: RebindRequestStatus
    review_chat_id: int | None
    review_message_id: int | None
    reviewed_by: int | None
    reviewed_at: datetime | None
    created_at: datetime | None


_UNSET = object()


DEFAULT_PUBLIC_SETTINGS = {
    "registration_open": "false",
    "registration_slots": "0",
    "server_lines": "暂未设置服务器线路。",
    "checkin_enabled": "true",
    "checkin_min_points": "1",
    "checkin_max_points": "10",
    "active_retention_enabled": "true",
    "active_retention_window_days": "30",
    "active_retention_extension_days": "30",
    "points_renewal_enabled": "true",
    "points_renewal_cost_points": "100",
    "points_renewal_extension_days": "30",
    "points_unban_enabled": "false",
    "points_unban_cost_points": "100",
}

DEFAULT_SYSTEM_SETTINGS = {
    "default_register_days": "30",
    "panel_photo_path": "",
    "rebind_review_chat_id": "",
    "main_group_chat_id": "",
    "main_group_link": "",
    "registration_announcement_chat_id": "",
    "registration_announcement_message_id": "",
    "disabled_delete_after_days": "0",
}


class MembershipService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        abs_client,
        *,
        registration_queue_delay_seconds: float = 2.0,
    ):
        self.session_factory = session_factory
        self.abs_client = abs_client
        self.registration_queue_delay_seconds = max(0.0, float(registration_queue_delay_seconds))
        self.default_settings = DEFAULT_PUBLIC_SETTINGS | DEFAULT_SYSTEM_SETTINGS
        self._registration_lock = _REGISTRATION_LOCK
        self._registration_queue_process_lock = _REGISTRATION_QUEUE_PROCESS_LOCK

    async def is_initialized(self) -> bool:
        async with self.session_factory() as session:
            rows = (await session.scalars(select(BotSetting))).all()
            return len(rows) > 0

    async def check_server_reachable(self) -> bool:
        """探测 ABS 服务器是否可达，不可达时仅记录日志。"""
        reachable = await self.abs_client.ping()
        if not reachable:
            logger.warning("ABS 服务器探活失败，服务器当前不可达")
        return reachable

    async def get_public_settings(self) -> PublicSettings:
        async with self.session_factory() as session:
            values = await self._settings_map(session)
        return self._public_settings(values)

    async def get_system_settings(self) -> SystemSettings:
        async with self.session_factory() as session:
            values = await self._settings_map(session)
        return self._system_settings(values)

    async def set_system_settings(
        self,
        *,
        default_register_days: int | object = _UNSET,
        panel_photo_path: str | None | object = _UNSET,
        rebind_review_chat_id: int | None | object = _UNSET,
        main_group_chat_id: int | None | object = _UNSET,
        main_group_link: str | None | object = _UNSET,
        disabled_delete_after_days: int | object = _UNSET,
    ) -> SystemSettings:
        async with self.session_factory() as session:
            async with session.begin():
                if default_register_days is not _UNSET:
                    days = int(default_register_days)
                    if days < 1:
                        raise ValueError("默认注册天数必须大于 0")
                    await self._set_setting(session, "default_register_days", str(days))
                if panel_photo_path is not _UNSET:
                    path = str(panel_photo_path).strip() if panel_photo_path is not None else ""
                    await self._set_setting(session, "panel_photo_path", path)
                if rebind_review_chat_id is not _UNSET:
                    chat_id = "" if rebind_review_chat_id is None else str(int(rebind_review_chat_id))
                    await self._set_setting(session, "rebind_review_chat_id", chat_id)
                if main_group_chat_id is not _UNSET:
                    chat_id = "" if main_group_chat_id is None else str(int(main_group_chat_id))
                    await self._set_setting(session, "main_group_chat_id", chat_id)
                if main_group_link is not _UNSET:
                    link = str(main_group_link).strip() if main_group_link is not None else ""
                    await self._set_setting(session, "main_group_link", link)
                if disabled_delete_after_days is not _UNSET:
                    days = int(disabled_delete_after_days)
                    if days < 0:
                        raise ValueError("自动删除天数不能小于 0")
                    await self._set_setting(session, "disabled_delete_after_days", str(days))
                values = await self._settings_map(session)
        return self._system_settings(values)

    async def set_registration(self, *, opened: bool, slots: int) -> PublicSettings:
        async with self.session_factory() as session:
            async with session.begin():
                await self._set_setting(session, "registration_open", _bool_text(opened))
                await self._set_setting(session, "registration_slots", str(max(0, slots)))
                values = await self._settings_map(session)
        return self._public_settings(values)

    async def get_registration_announcement_message(self) -> tuple[int | None, int | None]:
        async with self.session_factory() as session:
            values = await self._settings_map(session)
        raw_chat_id = values.get("registration_announcement_chat_id", "").strip()
        raw_message_id = values.get("registration_announcement_message_id", "").strip()
        return (
            int(raw_chat_id) if raw_chat_id else None,
            int(raw_message_id) if raw_message_id else None,
        )

    async def set_registration_announcement_message(
        self,
        *,
        chat_id: int | None,
        message_id: int | None,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                await self._set_setting(
                    session,
                    "registration_announcement_chat_id",
                    "" if chat_id is None else str(int(chat_id)),
                )
                await self._set_setting(
                    session,
                    "registration_announcement_message_id",
                    "" if message_id is None else str(int(message_id)),
                )

    async def set_server_lines(self, text: str) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                await self._set_setting(session, "server_lines", text)

    async def set_checkin(
        self,
        *,
        enabled: bool,
        min_points: int | None = None,
        max_points: int | None = None,
        points: int | None = None,
    ) -> PublicSettings:
        if points is not None:
            min_points = points
            max_points = points
        if min_points is None or max_points is None:
            raise ValueError("签到积分范围不能为空")
        if min_points < 0 or max_points < 0:
            raise ValueError("签到积分不能小于 0")
        if min_points > max_points:
            raise ValueError("签到积分最小值不能大于最大值")
        async with self.session_factory() as session:
            async with session.begin():
                await self._set_setting(session, "checkin_enabled", _bool_text(enabled))
                await self._set_setting(session, "checkin_min_points", str(min_points))
                await self._set_setting(session, "checkin_max_points", str(max_points))
                values = await self._settings_map(session)
        return self._public_settings(values)

    async def set_active_retention(
        self, *, enabled: bool, window_days: int, extension_days: int
    ) -> PublicSettings:
        async with self.session_factory() as session:
            async with session.begin():
                await self._set_setting(session, "active_retention_enabled", _bool_text(enabled))
                await self._set_setting(
                    session, "active_retention_window_days", str(max(1, window_days))
                )
                await self._set_setting(
                    session, "active_retention_extension_days", str(max(1, extension_days))
                )
                values = await self._settings_map(session)
        return self._public_settings(values)

    async def set_points_renewal(
        self, *, enabled: bool, cost_points: int, extension_days: int
    ) -> PublicSettings:
        async with self.session_factory() as session:
            async with session.begin():
                await self._set_setting(session, "points_renewal_enabled", _bool_text(enabled))
                await self._set_setting(
                    session, "points_renewal_cost_points", str(max(1, cost_points))
                )
                await self._set_setting(
                    session, "points_renewal_extension_days", str(max(1, extension_days))
                )
                values = await self._settings_map(session)
        return self._public_settings(values)

    async def set_points_unban(self, *, enabled: bool, cost_points: int) -> PublicSettings:
        async with self.session_factory() as session:
            async with session.begin():
                await self._set_setting(session, "points_unban_enabled", _bool_text(enabled))
                await self._set_setting(
                    session, "points_unban_cost_points", str(max(1, cost_points))
                )
                values = await self._settings_map(session)
        return self._public_settings(values)

    async def self_unban_by_points(self, telegram_id: int) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                user = await self._get_or_create_user(session, telegram_id)
                values = await self._settings_map(session)
                settings = self._public_settings(values)
                if not settings.points_unban_enabled:
                    raise ValueError("积分解禁功能未开启")
                if not user.is_disabled:
                    raise ValueError("账号未被禁用")
                cost = settings.points_unban_cost_points
                if user.points < cost:
                    raise InsufficientPointsError(current=user.points, needed=cost)
                user.points -= cost
                user.is_disabled = False
                user.disabled_at = None
                if user.abs_user_id:
                    await self._restore_abs_user(session, user)

    async def get_profile(self, telegram_id: int) -> TgUser:
        async with self.session_factory() as session:
            async with session.begin():
                return _normalize_user_datetimes(await self._get_or_create_user(session, telegram_id))

    async def list_users(self, *, whitelisted: bool = False, offset: int = 0, limit: int = 10):
        async with self.session_factory() as session:
            stmt: Select = select(TgUser).order_by(TgUser.telegram_id).offset(offset).limit(limit)
            if whitelisted:
                stmt = stmt.where(TgUser.is_whitelisted.is_(True))
            return [_normalize_user_datetimes(user) for user in (await session.scalars(stmt)).all()]

    async def grant_registration(
        self,
        telegram_id: int,
        *,
        credits: int,
        days: int | None = None,
        now: datetime | None = None,
    ) -> TgUser:
        now = ensure_utc(now) or utc_now()
        async with self.session_factory() as session:
            async with session.begin():
                user = await self._get_or_create_user(session, telegram_id)
                if credits > 0:
                    user.registration_credits = 1
                    if user.abs_user_id is None and days is None:
                        system = self._system_settings(await self._settings_map(session))
                        user.renewal_days = system.default_register_days
                if days is not None:
                    if user.abs_user_id is None:
                        user.renewal_days = days
                    else:
                        user.expires_at = _extend_from(user.expires_at, days, now)
                        user.renewal_days = None
                        if user.is_disabled and user.abs_user_id:
                            await self._restore_abs_user(session, user)
                            user.is_disabled = False
                            user.disabled_at = None
                return _normalize_user_datetimes(user)

    async def admin_adjust_points(self, telegram_id: int, *, delta: int) -> TgUser:
        async with self.session_factory() as session:
            async with session.begin():
                user = await self._get_or_create_user(session, telegram_id)
                user.points = max(0, user.points + delta)
                return _normalize_user_datetimes(user)

    async def admin_adjust_expiry(
        self,
        telegram_id: int,
        *,
        delta: int,
        now: datetime | None = None,
    ) -> TgUser:
        now = ensure_utc(now) or utc_now()
        async with self.session_factory() as session:
            async with session.begin():
                user = await self._get_or_create_user(session, telegram_id)
                base = ensure_utc(user.expires_at) or now
                user.expires_at = base + timedelta(days=delta)
                user.renewal_days = None
                if user.is_disabled and user.abs_user_id and user.expires_at > now:
                    await self._restore_abs_user(session, user)
                    user.is_disabled = False
                    user.disabled_at = None
                return _normalize_user_datetimes(user)

    async def set_whitelist(self, telegram_id: int, enabled: bool) -> TgUser:
        async with self.session_factory() as session:
            async with session.begin():
                user = await self._get_or_create_user(session, telegram_id)
                user.is_whitelisted = enabled
                if enabled and user.is_disabled and user.abs_user_id:
                    await self._restore_abs_user(session, user)
                    user.is_disabled = False
                    user.disabled_at = None
                return _normalize_user_datetimes(user)

    async def checkin(self, telegram_id: int, *, today: date | None = None) -> CheckinResult:
        today = today or date.today()
        async with self.session_factory() as session:
            async with session.begin():
                settings = self._public_settings(await self._settings_map(session))
                if not settings.checkin_enabled:
                    raise ValueError("签到未开放")
                user = await self._get_or_create_user(session, telegram_id)
                if user.last_checkin_date == today:
                    return CheckinResult(user.points, 0, True)
                user.last_checkin_date = today
                awarded = random.randint(settings.checkin_min_points, settings.checkin_max_points)
                user.points += awarded
                return CheckinResult(user.points, awarded, False)

    async def create_redeem_code(
        self,
        *,
        code: str | None = None,
        code_type: RedeemCodeType,
        days: int | None,
        max_uses: int,
        created_by: int | None = None,
    ) -> RedeemCode:
        code = normalize_code(code or generate_code(prefix=f"{code_type.value[:3]}-"))
        async with self.session_factory() as session:
            async with session.begin():
                redeem = RedeemCode(
                    code=code,
                    code_type=code_type,
                    days=days,
                    max_uses=1,
                    created_by=created_by,
                )
                session.add(redeem)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise ValueError("兑换码已存在") from exc
                return redeem

    async def create_redeem_codes(
        self,
        *,
        code_type: RedeemCodeType,
        days: int | None,
        count: int = 1,
        created_by: int | None = None,
    ) -> list[RedeemCode]:
        if count < 1:
            raise ValueError("生成数量必须大于 0")
        if count > MAX_REDEEM_CODES_PER_BATCH:
            raise ValueError(f"单次最多生成 {MAX_REDEEM_CODES_PER_BATCH} 个兑换码")

        codes = []
        for _ in range(count):
            codes.append(
                await self.create_redeem_code(
                    code=None,
                    code_type=code_type,
                    days=days,
                    max_uses=1,
                    created_by=created_by,
                )
            )
        return codes

    async def list_redeem_codes(
        self,
        *,
        code_type: RedeemCodeType | None = None,
        offset: int = 0,
        limit: int = 10,
        usable: bool = False,
    ):
        async with self.session_factory() as session:
            stmt = select(RedeemCode).order_by(RedeemCode.id.desc()).offset(offset).limit(limit)
            if code_type is not None:
                stmt = stmt.where(RedeemCode.code_type == code_type)
            if usable:
                now = utc_now()
                stmt = stmt.where(
                    RedeemCode.is_active.is_(True),
                    RedeemCode.used_count < RedeemCode.max_uses,
                    or_(
                        RedeemCode.expires_at.is_(None),
                        RedeemCode.expires_at >= now,
                    ),
                )
            return list((await session.scalars(stmt)).all())

    async def set_redeem_code_active(self, code_id: int, active: bool) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                redeem = await session.get(RedeemCode, code_id)
                if redeem is None:
                    raise ValueError("兑换码不存在")
                redeem.is_active = active

    async def delete_redeem_code(self, code_id: int) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                redeem = await session.get(RedeemCode, code_id)
                if redeem is None:
                    raise ValueError("兑换码不存在")
                await session.execute(delete(RedeemCodeUse).where(RedeemCodeUse.code_id == code_id))
                await session.delete(redeem)

    async def delete_redeem_codes_bulk(
        self,
        *,
        code_type: RedeemCodeType,
        used: bool | None = None,
        ids: list[int] | None = None,
    ) -> int:
        """Delete redeem codes in bulk. Returns number of deleted codes.

        used=True  → only delete fully-used codes (used_count >= max_uses)
        used=False → only delete unused codes (used_count == 0)
        used=None  → delete all codes of the given type (or all provided ids)
        ids        → restrict deletion to these specific code ids
        """
        async with self.session_factory() as session:
            async with session.begin():
                stmt = select(RedeemCode.id).where(RedeemCode.code_type == code_type)
                if used is True:
                    stmt = stmt.where(RedeemCode.used_count >= RedeemCode.max_uses)
                elif used is False:
                    stmt = stmt.where(RedeemCode.used_count == 0)
                if ids is not None:
                    stmt = stmt.where(RedeemCode.id.in_(ids))
                target_ids = list((await session.scalars(stmt)).all())
                if not target_ids:
                    return 0
                await session.execute(
                    delete(RedeemCodeUse).where(RedeemCodeUse.code_id.in_(target_ids))
                )
                result = await session.execute(
                    delete(RedeemCode).where(RedeemCode.id.in_(target_ids))
                )
                return result.rowcount

    async def redeem_code(self, telegram_id: int, code: str, now: datetime | None = None) -> RedeemResult:
        now = ensure_utc(now) or utc_now()
        normalized = normalize_code(code)
        async with self.session_factory() as session:
            async with session.begin():
                user = await self._get_or_create_user(session, telegram_id)
                redeem = await session.scalar(
                    select(RedeemCode).where(RedeemCode.code == normalized).with_for_update()
                )
                if redeem is None:
                    raise ValueError("兑换码无效")
                if redeem.used_count >= redeem.max_uses:
                    redeem.is_active = False
                    raise ValueError("兑换码已用完")
                if not redeem.is_active:
                    raise ValueError("兑换码无效")
                if redeem.expires_at is not None and ensure_utc(redeem.expires_at) < now:
                    raise ValueError("兑换码已过期")
                existing = await session.scalar(
                    select(RedeemCodeUse).where(
                        RedeemCodeUse.code_id == redeem.id,
                        RedeemCodeUse.telegram_id == telegram_id,
                    )
                )
                if existing is not None:
                    raise ValueError("你已经使用过这个兑换码")

                session.add(RedeemCodeUse(code_id=redeem.id, telegram_id=telegram_id))
                redeem.used_count += 1
                if redeem.used_count >= redeem.max_uses:
                    redeem.is_active = False

                message = "兑换成功"
                if redeem.code_type == RedeemCodeType.REGISTRATION:
                    if user.abs_user_id or user.registration_credits > 0:
                        raise ValueError("你已有账号或注册资格，无法使用注册码")
                    user.registration_credits = 1
                    user.renewal_days = redeem.days
                    message = "已获得注册资格"
                elif redeem.code_type == RedeemCodeType.RENEWAL:
                    if not redeem.days:
                        raise ValueError("续期码缺少天数")
                    if user.abs_user_id:
                        user.expires_at = _extend_from(user.expires_at, redeem.days, now)
                        user.renewal_days = None
                        if user.is_disabled:
                            await self._restore_abs_user(session, user)
                            user.is_disabled = False
                            user.disabled_at = None
                        message = f"已续期 {redeem.days} 天"
                    else:
                        system = self._system_settings(await self._settings_map(session))
                        user.renewal_days = (user.renewal_days or system.default_register_days) + redeem.days
                        message = f"已增加续期天数 {redeem.days} 天"
                elif redeem.code_type == RedeemCodeType.WHITELIST:
                    user.is_whitelisted = True
                    if user.is_disabled and user.abs_user_id:
                        await self._restore_abs_user(session, user)
                        user.is_disabled = False
                        user.disabled_at = None
                    message = "已加入白名单"

                return RedeemResult(
                    message=message,
                    registration_credits=user.registration_credits,
                    expires_at=ensure_utc(user.expires_at),
                    is_whitelisted=user.is_whitelisted,
                    points=user.points,
                )

    async def enqueue_registration(self, telegram_id: int, abs_username: str) -> int:
        clean_username = abs_username.strip()
        if not clean_username:
            raise ValueError("用户名不能为空")
        async with self._registration_lock:
            async with self.session_factory() as session:
                async with session.begin():
                    user = await self._get_or_create_user(session, telegram_id)
                    if user.abs_user_id:
                        raise ValueError("你已经创建过账号")

                    existing = await session.scalar(
                        select(RegistrationQueue).where(
                            RegistrationQueue.telegram_id == telegram_id,
                            RegistrationQueue.status.in_(ACTIVE_REGISTRATION_QUEUE_STATUSES),
                        )
                    )
                    if existing is not None:
                        return await self._queue_position(session, existing.id)

                    count = await session.scalar(
                        select(func.count()).select_from(RegistrationQueue).where(
                            RegistrationQueue.status.in_(ACTIVE_REGISTRATION_QUEUE_STATUSES)
                        )
                    )
                    position = int(count or 0) + 1
                    queue_item = RegistrationQueue(
                        telegram_id=telegram_id,
                        abs_username=clean_username,
                        status=RegistrationQueueStatus.PENDING,
                        position=position,
                    )
                    session.add(queue_item)
                    await session.flush()
                    return position

    async def get_queue_position(self, telegram_id: int) -> int | None:
        async with self.session_factory() as session:
            item = await session.scalar(
                select(RegistrationQueue).where(
                    RegistrationQueue.telegram_id == telegram_id,
                    RegistrationQueue.status.in_(ACTIVE_REGISTRATION_QUEUE_STATUSES),
                )
            )
            if item is None:
                return None
            return await self._queue_position(session, item.id)

    async def _queue_position(self, session, queue_id: int) -> int:
        item = await session.get(RegistrationQueue, queue_id)
        if item is None:
            return 0
        before = await session.scalar(
            select(func.count()).select_from(RegistrationQueue).where(
                RegistrationQueue.status.in_(ACTIVE_REGISTRATION_QUEUE_STATUSES),
                or_(
                    RegistrationQueue.created_at < item.created_at,
                    (RegistrationQueue.created_at == item.created_at)
                    & (RegistrationQueue.id <= item.id),
                ),
            )
        )
        return int(before or 0)

    async def process_next_registration(self) -> RegistrationQueueProcessResult | None:
        async with self._registration_queue_process_lock:
            return await self._process_next_registration_unlocked()

    async def get_next_registration_notification(self) -> RegistrationQueueProcessResult | None:
        async with self.session_factory() as session:
            item = await session.scalar(
                select(RegistrationQueue)
                .where(
                    RegistrationQueue.status.in_(
                        (RegistrationQueueStatus.DONE, RegistrationQueueStatus.FAILED)
                    ),
                    RegistrationQueue.notification_delivered.is_(False),
                )
                .order_by(RegistrationQueue.processed_at, RegistrationQueue.created_at, RegistrationQueue.id)
                .limit(1)
            )
            if item is None:
                return None
            if item.status == RegistrationQueueStatus.DONE:
                return RegistrationQueueProcessResult(
                    queue_id=item.id,
                    telegram_id=item.telegram_id,
                    success=True,
                    username=item.abs_username,
                    initial_password=item.result_password,
                    expires_at=ensure_utc(item.result_expires_at),
                )
            return RegistrationQueueProcessResult(
                queue_id=item.id,
                telegram_id=item.telegram_id,
                success=False,
                username=item.abs_username,
                error_message=item.error_message,
            )

    async def mark_registration_queue_notified(self, queue_id: int) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                item = await session.get(RegistrationQueue, queue_id)
                if item is not None:
                    item.notification_delivered = True

    async def _process_next_registration_unlocked(self) -> RegistrationQueueProcessResult | None:
        async with self._registration_lock:
            async with self.session_factory() as session:
                async with session.begin():
                    item = await session.scalar(
                        select(RegistrationQueue)
                        .where(RegistrationQueue.status == RegistrationQueueStatus.PENDING)
                        .order_by(RegistrationQueue.created_at, RegistrationQueue.id)
                        .limit(1)
                    )
                    if item is None:
                        return None
                    item.status = RegistrationQueueStatus.PROCESSING
                    queue_id = item.id
                    telegram_id = item.telegram_id
                    username = item.abs_username
                    reserved_password = item.result_password
                    user = await session.scalar(select(TgUser).where(TgUser.telegram_id == telegram_id))
                    if user is not None and user.abs_user_id and user.abs_password:
                        item.status = RegistrationQueueStatus.DONE
                        item.result_password = user.abs_password
                        item.result_expires_at = user.expires_at
                        item.notification_delivered = False
                        item.processed_at = utc_now()
                        await session.flush()
                        await session.refresh(item)
                        return RegistrationQueueProcessResult(
                            queue_id=queue_id,
                            telegram_id=telegram_id,
                            success=True,
                            username=user.abs_username,
                            initial_password=user.abs_password,
                            expires_at=ensure_utc(item.result_expires_at),
                            registration_closed=False,
                        )

        before = await self.get_public_settings()
        if self.registration_queue_delay_seconds > 0:
            await asyncio.sleep(self.registration_queue_delay_seconds)
        try:
            if reserved_password:
                created = await self._complete_reserved_registration(
                    telegram_id,
                    username,
                    password=reserved_password,
                )
            else:
                created = await self.create_account_from_registration(
                    telegram_id,
                    username,
                    reservation_queue_id=queue_id,
                )
        except Exception as exc:
            error_message = str(exc) or exc.__class__.__name__
            async with self.session_factory() as session:
                async with session.begin():
                    item = await session.get(RegistrationQueue, queue_id)
                    if item is not None:
                        item.status = RegistrationQueueStatus.FAILED
                        item.error_message = error_message
                        item.notification_delivered = False
                        item.processed_at = utc_now()
            return RegistrationQueueProcessResult(
                queue_id=queue_id,
                telegram_id=telegram_id,
                success=False,
                username=username,
                error_message=error_message,
            )

        after = await self.get_public_settings()
        result_expires_at = ensure_utc(created.expires_at)
        async with self.session_factory() as session:
            async with session.begin():
                item = await session.get(RegistrationQueue, queue_id)
                if item is not None:
                    item.status = RegistrationQueueStatus.DONE
                    item.result_password = created.initial_password
                    item.result_expires_at = created.expires_at
                    item.notification_delivered = False
                    item.processed_at = utc_now()
                    await session.flush()
                    await session.refresh(item)
                    result_expires_at = ensure_utc(item.result_expires_at)

        return RegistrationQueueProcessResult(
            queue_id=queue_id,
            telegram_id=telegram_id,
            success=True,
            username=created.username,
            initial_password=created.initial_password,
            expires_at=result_expires_at,
            registration_closed=(
                before.registration_open
                and before.registration_slots == 1
                and not after.registration_open
                and after.registration_slots == 0
            ),
        )

    async def reset_stuck_queue_items(self) -> int:
        async with self.session_factory() as session:
            async with session.begin():
                items = list(
                    (
                        await session.scalars(
                            select(RegistrationQueue).where(
                                RegistrationQueue.status == RegistrationQueueStatus.PROCESSING
                            )
                        )
                    ).all()
                )
                for item in items:
                    item.status = RegistrationQueueStatus.PENDING
                return len(items)

    async def create_account_from_registration(
        self,
        telegram_id: int,
        username: str,
        *,
        now: datetime | None = None,
        reservation_queue_id: int | None = None,
    ) -> AccountCreationResult:
        now = ensure_utc(now) or utc_now()
        clean_username = username.strip()
        if not clean_username:
            raise ValueError("用户名不能为空")
        password = generate_password()
        consumed_slot = False
        consumed_credit = False
        assigned_expiration = False
        old_expires_at = None
        old_renewal_days = None
        expires_at = None
        async with self._registration_lock:
            async with self.session_factory() as session:
                async with session.begin():
                    values = await self._settings_map(session)
                    settings = self._public_settings(values)
                    user = await self._get_or_create_user(session, telegram_id)
                    if user.abs_user_id:
                        raise ValueError("你已经创建过账号")

                    if user.is_whitelisted:
                        pass
                    elif user.registration_credits > 0:
                        user.registration_credits = 0
                        consumed_credit = True
                    elif settings.registration_open and settings.registration_slots > 0:
                        slots_remaining = settings.registration_slots - 1
                        await self._set_setting(session, "registration_slots", str(slots_remaining))
                        consumed_slot = True
                        if slots_remaining == 0:
                            await self._set_setting(session, "registration_open", "false")
                    else:
                        raise ValueError("当前没有可用注册资格")

                    if not user.is_whitelisted:
                        system = self._system_settings(values)
                        days = (user.renewal_days or 0) or system.default_register_days
                        old_expires_at = ensure_utc(user.expires_at)
                        user.expires_at = _extend_from(user.expires_at, days, now)
                        assigned_expiration = True
                    old_renewal_days = user.renewal_days
                    user.renewal_days = None
                    expires_at = ensure_utc(user.expires_at)
                    if reservation_queue_id is not None:
                        queue_item = await session.get(RegistrationQueue, reservation_queue_id)
                        if queue_item is not None:
                            queue_item.result_password = password
                            queue_item.result_expires_at = expires_at

            try:
                abs_user = await self.abs_client.create_user(clean_username, password)
            except Exception:
                await self._compensate_registration_consumption(
                    telegram_id,
                    consumed_slot=consumed_slot,
                    consumed_credit=consumed_credit,
                    assigned_expiration=assigned_expiration,
                    old_expires_at=old_expires_at,
                    old_renewal_days=old_renewal_days,
                )
                raise

            try:
                async with self.session_factory() as session:
                    async with session.begin():
                        user = await self._get_or_create_user(session, telegram_id)
                        if user.abs_user_id:
                            raise ValueError("你已经创建过账号")
                        user.abs_user_id = abs_user["id"]
                        user.abs_username = abs_user.get("username", clean_username)
                        user.abs_password = password
                        user.is_disabled = False
                        user.disabled_at = None
                        return AccountCreationResult(
                            abs_user_id=user.abs_user_id,
                            username=user.abs_username,
                            initial_password=password,
                            expires_at=expires_at,
                        )
            except Exception:
                await self._compensate_registration_consumption(
                    telegram_id,
                    consumed_slot=consumed_slot,
                    consumed_credit=consumed_credit,
                    assigned_expiration=assigned_expiration,
                    old_expires_at=old_expires_at,
                )
                try:
                    await self.abs_client.delete_user(abs_user["id"])
                except Exception:
                    logger.exception("清理已创建的 ABS 用户失败：%s", abs_user.get("id"))
                raise

    async def _complete_reserved_registration(
        self,
        telegram_id: int,
        username: str,
        *,
        password: str,
    ) -> AccountCreationResult:
        clean_username = username.strip()
        if not clean_username:
            raise ValueError("用户名不能为空")
        if not password:
            raise ValueError("预留注册缺少初始密码")

        abs_user = await self._find_abs_user_by_username(clean_username)
        created_abs_user_id: str | None = None
        if abs_user is None:
            abs_user = await self.abs_client.create_user(clean_username, password)
            created_abs_user_id = str(abs_user.get("id") or "").strip() or None

        abs_user_id = str(abs_user.get("id") or "").strip()
        if not abs_user_id:
            raise AudiobookshelfError("Audiobookshelf 创建用户响应缺少用户 ID")
        abs_username = str(abs_user.get("username") or clean_username).strip() or clean_username

        try:
            async with self.session_factory() as session:
                async with session.begin():
                    user = await self._get_or_create_user(session, telegram_id)
                    if user.abs_user_id:
                        return AccountCreationResult(
                            abs_user_id=user.abs_user_id,
                            username=user.abs_username or abs_username,
                            initial_password=user.abs_password or password,
                            expires_at=ensure_utc(user.expires_at),
                        )
                    user.abs_user_id = abs_user_id
                    user.abs_username = abs_username
                    user.abs_password = password
                    user.is_disabled = False
                    user.disabled_at = None
                    return AccountCreationResult(
                        abs_user_id=user.abs_user_id,
                        username=user.abs_username,
                        initial_password=password,
                        expires_at=ensure_utc(user.expires_at),
                    )
        except Exception:
            if created_abs_user_id is not None:
                try:
                    await self.abs_client.delete_user(created_abs_user_id)
                except Exception:
                    logger.exception("清理已创建的 ABS 用户失败：%s", created_abs_user_id)
            raise

    async def _compensate_registration_consumption(
        self,
        telegram_id: int,
        *,
        consumed_slot: bool,
        consumed_credit: bool,
        assigned_expiration: bool,
        old_expires_at: datetime | None = None,
        old_renewal_days: int | None = None,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                if consumed_slot:
                    values = await self._settings_map(session)
                    slots = max(0, int(values["registration_slots"])) + 1
                    await self._set_setting(session, "registration_slots", str(slots))
                    await self._set_setting(session, "registration_open", "true")
                if consumed_credit or assigned_expiration:
                    user = await self._get_or_create_user(session, telegram_id)
                    if consumed_credit:
                        user.registration_credits = 1
                    if assigned_expiration and user.abs_user_id is None:
                        user.expires_at = old_expires_at
                        user.renewal_days = old_renewal_days

    async def bind_existing_account(
        self,
        telegram_id: int,
        username: str,
        password: str,
        *,
        now: datetime | None = None,
    ) -> AccountBindingResult:
        now = ensure_utc(now) or utc_now()
        abs_user_id, abs_username = await self._authenticate_existing_account(username, password)
        async with self.session_factory() as session:
            async with session.begin():
                system = self._system_settings(await self._settings_map(session))
                user = await self._get_or_create_user(session, telegram_id)
                if user.abs_user_id:
                    raise ValueError("你已经绑定了账号")
                existing = await self._get_user_by_abs_account(
                    session, abs_user_id=abs_user_id, abs_username=abs_username
                )
                if existing is not None and existing.telegram_id != telegram_id:
                    raise ValueError("该 ABS 账号已经绑定到其他 Telegram 用户，请使用申请换绑")

                if not user.is_whitelisted:
                    days = (user.renewal_days or 0) or system.default_register_days
                    user.expires_at = _extend_from(user.expires_at, days, now)
                user.renewal_days = None
                user.abs_user_id = abs_user_id
                user.abs_username = abs_username
                user.abs_password = password
                user.is_disabled = False
                user.disabled_at = None
                try:
                    await session.flush()
                except IntegrityError as exc:
                    raise ValueError("该 ABS 账号已经被绑定，请使用申请换绑") from exc
                return AccountBindingResult(
                    abs_user_id=user.abs_user_id,
                    username=user.abs_username,
                    expires_at=ensure_utc(user.expires_at),
                )

    async def create_rebind_request(
        self,
        telegram_id: int,
        username: str,
        password: str,
        *,
        now: datetime | None = None,
    ) -> RebindRequestSnapshot:
        now = ensure_utc(now) or utc_now()
        abs_user_id, abs_username = await self._authenticate_existing_account(username, password)
        snapshot: RebindRequestSnapshot
        async with self.session_factory() as session:
            async with session.begin():
                requester = await self._get_or_create_user(session, telegram_id)
                if requester.abs_user_id:
                    raise ValueError("你已经绑定了账号")
                current = await self._get_user_by_abs_account(
                    session, abs_user_id=abs_user_id, abs_username=abs_username
                )
                if current is None:
                    raise ValueError("该 ABS 账号尚未绑定，可直接使用绑定账号")
                if current.telegram_id == telegram_id:
                    raise ValueError("该 ABS 账号已经绑定到你当前 Telegram")
                request = RebindRequest(
                    requester_telegram_id=telegram_id,
                    abs_user_id=abs_user_id,
                    abs_username=abs_username,
                    current_telegram_id=current.telegram_id,
                    status=RebindRequestStatus.PENDING,
                    created_at=now,
                )
                session.add(request)
                await session.flush()
                snapshot = _snapshot_rebind_request(request)
        return snapshot

    async def set_rebind_review_message(
        self, request_id: int, *, chat_id: int, message_id: int
    ) -> RebindRequestSnapshot:
        async with self.session_factory() as session:
            async with session.begin():
                request = await self._get_rebind_request_for_update(session, request_id)
                request.review_chat_id = chat_id
                request.review_message_id = message_id
                return _snapshot_rebind_request(request)

    async def approve_rebind_request(
        self,
        request_id: int,
        *,
        reviewer_telegram_id: int,
        now: datetime | None = None,
    ) -> RebindRequestSnapshot:
        now = ensure_utc(now) or utc_now()
        async with self.session_factory() as session:
            async with session.begin():
                system = self._system_settings(await self._settings_map(session))
                request = await self._get_rebind_request_for_update(session, request_id)
                if request.status != RebindRequestStatus.PENDING:
                    raise ValueError("换绑申请已处理")

                requester = await self._get_or_create_user(session, request.requester_telegram_id)
                if requester.abs_user_id:
                    raise ValueError("申请人已经绑定了账号")

                current = await self._get_user_by_abs_account(
                    session,
                    abs_user_id=request.abs_user_id,
                    abs_username=request.abs_username,
                )
                if current is None:
                    requester.abs_user_id = request.abs_user_id
                    requester.abs_username = request.abs_username
                    if not requester.is_whitelisted:
                        days = (requester.renewal_days or 0) or system.default_register_days
                        requester.expires_at = _extend_from(requester.expires_at, days, now)
                    requester.renewal_days = None
                    requester.is_disabled = False
                    requester.disabled_at = None
                elif current.telegram_id == requester.telegram_id:
                    raise ValueError("申请人已经绑定了该账号")
                else:
                    await self._transfer_account_ownership(session, current, requester, request, now=now)

                request.status = RebindRequestStatus.APPROVED
                request.reviewed_by = reviewer_telegram_id
                request.reviewed_at = now
                await session.flush()
                return _snapshot_rebind_request(request)

    async def reject_rebind_request(
        self,
        request_id: int,
        *,
        reviewer_telegram_id: int,
        now: datetime | None = None,
    ) -> RebindRequestSnapshot:
        now = ensure_utc(now) or utc_now()
        async with self.session_factory() as session:
            async with session.begin():
                request = await self._get_rebind_request_for_update(session, request_id)
                if request.status != RebindRequestStatus.PENDING:
                    raise ValueError("换绑申请已处理")
                request.status = RebindRequestStatus.REJECTED
                request.reviewed_by = reviewer_telegram_id
                request.reviewed_at = now
                return _snapshot_rebind_request(request)

    async def reset_password(self, telegram_id: int) -> PasswordResetResult:
        password = generate_password()
        async with self.session_factory() as session:
            async with session.begin():
                user = await self._get_user_or_fail(session, telegram_id)
                if not user.abs_user_id or not user.abs_username:
                    raise ValueError("尚未创建账号")
                await self._reset_abs_password(session, user, password)
                user.abs_password = password
                return PasswordResetResult(user.abs_username, password)

    async def delete_account(self, telegram_id: int) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                user = await self._get_user_or_fail(session, telegram_id)
                if not user.abs_user_id:
                    raise ValueError("尚未创建账号")
                await self._delete_abs_user(session, user)
                user.abs_user_id = None
                user.abs_username = None
                user.abs_password = None
                user.expires_at = None
                user.last_seen_at = None
                user.last_played_at = None
                user.is_disabled = False
                user.disabled_at = None

    async def delete_user_and_account(self, telegram_id: int) -> bool:
        """Delete the ABS account (if any) and fully remove the TgUser record.

        Returns True if a user record was found and deleted, False otherwise.
        """
        async with self.session_factory() as session:
            async with session.begin():
                user = await session.scalar(
                    select(TgUser).where(TgUser.telegram_id == telegram_id)
                )
                if user is None:
                    return False
                if user.abs_user_id:
                    try:
                        await self._delete_abs_user(session, user)
                    except Exception:
                        logger.warning(
                            "退群清理：删除 ABS 账号失败 tg=%s abs=%s，继续删除本地记录",
                            telegram_id,
                            user.abs_user_id,
                        )
                await session.execute(
                    delete(RedeemCodeUse).where(RedeemCodeUse.telegram_id == telegram_id)
                )
                await session.execute(
                    delete(RebindRequest).where(
                        RebindRequest.requester_telegram_id == telegram_id
                    )
                )
                await session.execute(
                    delete(RegistrationQueue).where(
                        RegistrationQueue.telegram_id == telegram_id
                    )
                )
                await session.delete(user)
        logger.info("退群清理：已删除用户 tg=%s 的账号和全部记录", telegram_id)
        return True

    async def update_display_name(self, telegram_id: int, display_name: str) -> None:
        """Passively update tg_display_name for a user. Silently no-ops if user not found."""
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(TgUser)
                    .where(TgUser.telegram_id == telegram_id)
                    .values(tg_display_name=display_name)
                )

    async def sync_profile_activity(self, telegram_id: int) -> TgUser:
        async with self.session_factory() as session:
            async with session.begin():
                user = await self._get_or_create_user(session, telegram_id)
                if not user.abs_user_id:
                    return _normalize_user_datetimes(user)
                try:
                    user_data = await self._get_abs_user(session, user)
                    session_data = await self._get_abs_latest_listening_session(session, user)
                except AudiobookshelfNotFoundError:
                    logger.warning(
                        "ABS 用户在服务器上不存在，已清除本地关联：tg=%s abs_user_id=%s",
                        telegram_id,
                        user.abs_user_id,
                    )
                    user.abs_user_id = None
                    user.abs_username = None
                    user.last_seen_at = None
                    user.last_played_at = None
                    user.expires_at = None
                    user.disabled_at = None
                    return _normalize_user_datetimes(user)
                except AudiobookshelfError:
                    logger.warning(
                        "ABS 服务器不可达，跳过活动同步，返回缓存数据：tg=%s",
                        telegram_id,
                    )
                    return _normalize_user_datetimes(user)
                user.last_seen_at = from_millis(user_data.get("lastSeen"))
                user.last_played_at = from_millis(session_data.get("updatedAt")) if session_data else None
                return _normalize_user_datetimes(user)

    async def sync_all_activity(self) -> None:
        async with self.session_factory() as session:
            users = list(
                (
                    await session.scalars(
                        select(TgUser).where(TgUser.abs_user_id.is_not(None)).order_by(TgUser.id)
                    )
                ).all()
            )
        for user in users:
            await self.sync_profile_activity(user.telegram_id)

    async def process_activity_check(self, *, now: datetime | None = None) -> ActivityCheckResult:
        now = ensure_utc(now) or utc_now()
        await self.sync_all_activity()
        disabled: list[ActivityUserResult] = []
        deleted: list[ActivityUserResult] = []
        async with self.session_factory() as session:
            async with session.begin():
                values = await self._settings_map(session)
                settings = self._public_settings(values)
                system = self._system_settings(values)
                users = list(
                    (
                        await session.scalars(
                            select(TgUser).where(TgUser.abs_user_id.is_not(None))
                        )
                    ).all()
                )
                if not settings.active_retention_enabled:
                    logger.info(
                        "活跃检测：活跃保号功能已关闭，跳过禁用检查，共 %d 位用户",
                        len(users),
                    )
                else:
                    cutoff = now - timedelta(days=settings.active_retention_window_days)
                    logger.info(
                        "活跃检测开始：共 %d 位用户，活跃窗口 %d 天，截止时间 %s",
                        len(users),
                        settings.active_retention_window_days,
                        format_dt(cutoff),
                    )
                    for user in users:
                        # 已禁用 → 检查是否达到自动删除阈值
                        if user.is_disabled:
                            if self._should_auto_delete_disabled(user, system, now):
                                disabled_at = self._disabled_since(user)
                                logger.info(
                                    "用户 %s (tg=%d) 已禁用超过 %d 天，即将删除 | 禁用时间=%s",
                                    user.abs_username or user.abs_user_id,
                                    user.telegram_id,
                                    system.disabled_delete_after_days,
                                    format_dt(disabled_at),
                                )
                                await self._delete_abs_user(session, user)
                                deleted.append(
                                    ActivityUserResult(
                                        telegram_id=user.telegram_id,
                                        abs_user_id=user.abs_user_id,
                                        abs_username=user.abs_username,
                                        disabled_at=disabled_at,
                                        deleted_at=now,
                                    )
                                )
                                user.abs_user_id = None
                                user.abs_username = None
                                user.abs_password = None
                                user.expires_at = None
                                user.last_seen_at = None
                                user.last_played_at = None
                                user.is_disabled = False
                                user.disabled_at = None
                            else:
                                logger.debug(
                                    "用户 %s (tg=%d) 已禁用，尚未达到删除阈值，跳过 | 最后登录=%s 最后播放=%s",
                                    user.abs_username or user.abs_user_id,
                                    user.telegram_id,
                                    format_dt(user.last_seen_at),
                                    format_dt(user.last_played_at),
                                )
                            continue

                        if user.is_whitelisted:
                            logger.debug(
                                "用户 %s (tg=%d) 已白名单，跳过 | 最后登录=%s 最后播放=%s",
                                user.abs_username or user.abs_user_id,
                                user.telegram_id,
                                format_dt(user.last_seen_at),
                                format_dt(user.last_played_at),
                            )
                            continue

                        latest_activity = max_datetime(user.last_seen_at, user.last_played_at)
                        if latest_activity is not None and latest_activity >= cutoff:
                            logger.debug(
                                "用户 %s (tg=%d) 活跃正常 | 最后登录=%s 最后播放=%s 最近活跃=%s",
                                user.abs_username or user.abs_user_id,
                                user.telegram_id,
                                format_dt(user.last_seen_at),
                                format_dt(user.last_played_at),
                                format_dt(latest_activity),
                            )
                            continue

                        logger.info(
                            "用户 %s (tg=%d) 不活跃，即将禁用 | 最后登录=%s 最后播放=%s 最近活跃=%s",
                            user.abs_username or user.abs_user_id,
                            user.telegram_id,
                            format_dt(user.last_seen_at),
                            format_dt(user.last_played_at),
                            format_dt(latest_activity),
                        )
                        await self._disable_abs_user(session, user)
                        user.is_disabled = True
                        user.disabled_at = now
                        disabled.append(
                            ActivityUserResult(
                                telegram_id=user.telegram_id,
                                abs_user_id=user.abs_user_id,
                                abs_username=user.abs_username,
                                disabled_at=now,
                            )
                        )
                    logger.info(
                        "活跃检测完成：检测 %d 位，禁用 %d 位，删除 %d 位",
                        len(users),
                        len(disabled),
                        len(deleted),
                    )
        return ActivityCheckResult(total_synced=len(users), disabled=disabled, deleted=deleted)

    async def process_expirations(self, *, now: datetime | None = None) -> ExpirationProcessResult:
        now = ensure_utc(now) or utc_now()
        await self._backfill_legacy_disabled_at(now)
        await self.sync_all_activity()
        active_renewed: list[ExpirationUserResult] = []
        points_renewed: list[ExpirationUserResult] = []
        disabled: list[ExpirationUserResult] = []
        deleted: list[ExpirationUserResult] = []
        async with self.session_factory() as session:
            async with session.begin():
                values = await self._settings_map(session)
                settings = self._public_settings(values)
                system = self._system_settings(values)
                users = list(
                    (
                        await session.scalars(
                            select(TgUser).where(TgUser.abs_user_id.is_not(None))
                        )
                    ).all()
                )
                for user in users:
                    if self._should_auto_delete_disabled(user, system, now):
                        disabled_at = self._disabled_since(user)
                        await self._delete_abs_user(session, user)
                        deleted.append(
                            _expiration_user_result(
                                user,
                                disabled_at=disabled_at,
                                deleted_at=now,
                            )
                        )
                        user.abs_user_id = None
                        user.abs_username = None
                        user.abs_password = None
                        user.expires_at = None
                        user.last_seen_at = None
                        user.last_played_at = None
                        user.is_disabled = False
                        user.disabled_at = None
                        continue

                    if user.is_whitelisted:
                        continue
                    expires_at = ensure_utc(user.expires_at)
                    if expires_at is None or expires_at > now:
                        continue

                    if self._can_active_extend(user, settings, now):
                        user.expires_at = now + timedelta(days=settings.active_retention_extension_days)
                        user.renewal_days = None
                        if user.is_disabled and user.abs_user_id:
                            await self._restore_abs_user(session, user)
                            user.is_disabled = False
                            user.disabled_at = None
                        active_renewed.append(_expiration_user_result(user))
                        continue

                    if settings.points_renewal_enabled and user.points >= settings.points_renewal_cost_points:
                        user.points -= settings.points_renewal_cost_points
                        user.expires_at = now + timedelta(days=settings.points_renewal_extension_days)
                        user.renewal_days = None
                        if user.is_disabled and user.abs_user_id:
                            await self._restore_abs_user(session, user)
                            user.is_disabled = False
                            user.disabled_at = None
                        points_renewed.append(
                            _expiration_user_result(
                                user,
                                points_spent=settings.points_renewal_cost_points,
                            )
                        )
                        continue

                    if not user.is_disabled and user.abs_user_id:
                        await self._disable_abs_user(session, user)
                        user.is_disabled = True
                        user.disabled_at = now
                        disabled.append(_expiration_user_result(user, disabled_at=now))
        return ExpirationProcessResult(
            active_renewed=active_renewed,
            points_renewed=points_renewed,
            disabled=disabled,
            deleted=deleted,
        )

    def _should_auto_delete_disabled(
        self, user: TgUser, settings: SystemSettings, now: datetime
    ) -> bool:
        if (
            user.is_whitelisted
            or not user.is_disabled
            or not user.abs_user_id
            or settings.disabled_delete_after_days <= 0
        ):
            return False
        disabled_at = self._disabled_since(user)
        return disabled_at is not None and disabled_at <= now - timedelta(
            days=settings.disabled_delete_after_days
        )

    def _disabled_since(self, user: TgUser) -> datetime | None:
        return ensure_utc(user.disabled_at) or ensure_utc(user.updated_at)

    async def _backfill_legacy_disabled_at(self, now: datetime) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                users = list(
                    (
                        await session.scalars(
                            select(TgUser)
                            .where(TgUser.is_disabled.is_(True))
                            .where(TgUser.disabled_at.is_(None))
                            .where(TgUser.abs_user_id.is_not(None))
                        )
                    ).all()
                )
                for user in users:
                    user.disabled_at = ensure_utc(user.updated_at) or now

    def _can_active_extend(self, user: TgUser, settings: PublicSettings, now: datetime) -> bool:
        if not settings.active_retention_enabled:
            return False
        latest_activity = max_datetime(user.last_seen_at, user.last_played_at)
        return latest_activity is not None and latest_activity >= now - timedelta(
            days=settings.active_retention_window_days
        )

    async def _authenticate_existing_account(self, username: str, password: str) -> tuple[str, str]:
        clean_username = username.strip()
        if not clean_username:
            raise ValueError("用户名不能为空")
        if not password:
            raise ValueError("密码不能为空")
        abs_user = await self.abs_client.authenticate_user(clean_username, password)
        abs_user_id = str(abs_user.get("id") or "").strip()
        abs_username = str(abs_user.get("username") or clean_username).strip()
        if not abs_user_id:
            raise ValueError("Audiobookshelf 登录响应缺少用户 ID")
        if not abs_username:
            abs_username = clean_username
        return abs_user_id, abs_username

    async def _get_user_by_abs_account(
        self,
        session,
        *,
        abs_user_id: str,
        abs_username: str,
    ) -> TgUser | None:
        return await session.scalar(
            select(TgUser).where(
                or_(TgUser.abs_user_id == abs_user_id, TgUser.abs_username == abs_username)
            )
        )

    async def _get_rebind_request_for_update(self, session, request_id: int) -> RebindRequest:
        request = await session.scalar(
            select(RebindRequest).where(RebindRequest.id == request_id).with_for_update()
        )
        if request is None:
            raise ValueError("换绑申请不存在")
        return request

    async def _transfer_account_ownership(
        self,
        session,
        current: TgUser,
        requester: TgUser,
        request: RebindRequest,
        *,
        now: datetime,
    ) -> None:
        registration_credits = current.registration_credits
        points = current.points
        is_whitelisted = current.is_whitelisted
        expires_at = current.expires_at
        last_seen_at = current.last_seen_at
        last_played_at = current.last_played_at
        last_checkin_date = current.last_checkin_date
        is_disabled = current.is_disabled
        disabled_at = current.disabled_at
        abs_password = current.abs_password

        current.abs_user_id = None
        current.abs_username = None
        current.abs_password = None
        current.registration_credits = 0
        current.renewal_days = None
        current.points = 0
        current.is_whitelisted = False
        current.expires_at = None
        current.last_seen_at = None
        current.last_played_at = None
        current.last_checkin_date = None
        current.is_disabled = False
        current.disabled_at = None
        await session.flush()

        requester.abs_user_id = request.abs_user_id
        requester.abs_username = request.abs_username
        requester.abs_password = abs_password
        requester.registration_credits = 1 if requester.registration_credits or registration_credits else 0
        requester.points = points
        requester.is_whitelisted = requester.is_whitelisted or is_whitelisted

        base_expires = expires_at
        if requester.renewal_days:
            req_expires = _extend_from(None, requester.renewal_days, now)
            base_expires = max_datetime(base_expires, req_expires)
        requester.expires_at = max_datetime(requester.expires_at, base_expires)

        requester.renewal_days = None

        requester.last_seen_at = max_datetime(requester.last_seen_at, last_seen_at)
        requester.last_played_at = max_datetime(requester.last_played_at, last_played_at)
        requester.last_checkin_date = max_date(requester.last_checkin_date, last_checkin_date)
        requester.is_disabled = is_disabled
        requester.disabled_at = disabled_at if is_disabled else None

    async def _settings_map(self, session) -> dict[str, str]:
        rows = (await session.scalars(select(BotSetting))).all()
        row_values = {row.key: row.value for row in rows}
        if (
            "checkin_points" in row_values
            and "checkin_min_points" not in row_values
            and "checkin_max_points" not in row_values
        ):
            row_values["checkin_min_points"] = row_values["checkin_points"]
            row_values["checkin_max_points"] = row_values["checkin_points"]
        values = self.default_settings.copy()
        values.update(row_values)
        return values

    async def _set_setting(self, session, key: str, value: str) -> None:
        setting = await session.get(BotSetting, key)
        if setting is None:
            session.add(BotSetting(key=key, value=value))
        else:
            setting.value = value

    def _public_settings(self, values: dict[str, str]) -> PublicSettings:
        return PublicSettings(
            registration_open=_as_bool(values["registration_open"]),
            registration_slots=int(values["registration_slots"]),
            server_lines=values["server_lines"],
            checkin_enabled=_as_bool(values["checkin_enabled"]),
            checkin_min_points=int(values["checkin_min_points"]),
            checkin_max_points=int(values["checkin_max_points"]),
            active_retention_enabled=_as_bool(values["active_retention_enabled"]),
            active_retention_window_days=int(values["active_retention_window_days"]),
            active_retention_extension_days=int(values["active_retention_extension_days"]),
            points_renewal_enabled=_as_bool(values["points_renewal_enabled"]),
            points_renewal_cost_points=int(values["points_renewal_cost_points"]),
            points_renewal_extension_days=int(values["points_renewal_extension_days"]),
            points_unban_enabled=_as_bool(values["points_unban_enabled"]),
            points_unban_cost_points=int(values["points_unban_cost_points"]),
        )

    def _system_settings(self, values: dict[str, str]) -> SystemSettings:
        raw_panel_path = values.get("panel_photo_path", "").strip()
        raw_chat_id = values.get("rebind_review_chat_id", "").strip()
        raw_main_group_chat_id = values.get("main_group_chat_id", "").strip()
        raw_main_group_link = values.get("main_group_link", "").strip()
        return SystemSettings(
            default_register_days=max(1, int(values["default_register_days"])),
            panel_photo_path=raw_panel_path or None,
            rebind_review_chat_id=int(raw_chat_id) if raw_chat_id else None,
            main_group_chat_id=int(raw_main_group_chat_id) if raw_main_group_chat_id else None,
            main_group_link=raw_main_group_link or None,
            disabled_delete_after_days=max(0, int(values["disabled_delete_after_days"])),
        )

    async def _get_or_create_user(self, session, telegram_id: int) -> TgUser:
        user = await session.scalar(select(TgUser).where(TgUser.telegram_id == telegram_id))
        if user is not None:
            return user
        user = TgUser(telegram_id=telegram_id)
        session.add(user)
        await session.flush()
        return user

    async def _get_user_or_fail(self, session, telegram_id: int) -> TgUser:
        user = await session.scalar(select(TgUser).where(TgUser.telegram_id == telegram_id))
        if user is None:
            raise ValueError("用户不存在")
        return user

    async def _get_abs_user(self, session, user: TgUser) -> dict:
        return await self._call_abs_user_operation(session, user, self.abs_client.get_user)

    async def _get_abs_latest_listening_session(self, session, user: TgUser) -> dict | None:
        return await self._call_abs_user_operation(
            session,
            user,
            self.abs_client.get_latest_listening_session,
        )

    async def _reset_abs_password(self, session, user: TgUser, password: str) -> dict:
        return await self._call_abs_user_operation(
            session,
            user,
            lambda abs_user_id: self.abs_client.reset_password(abs_user_id, password),
        )

    async def _disable_abs_user(self, session, user: TgUser) -> dict | None:
        return await self._call_abs_user_operation(session, user, self.abs_client.disable_user)

    async def _restore_abs_user(self, session, user: TgUser) -> dict | None:
        return await self._call_abs_user_operation(session, user, self.abs_client.restore_user)

    async def _delete_abs_user(self, session, user: TgUser) -> dict | None:
        return await self._call_abs_user_operation(session, user, self.abs_client.delete_user)

    async def _call_abs_user_operation(self, session, user: TgUser, operation):
        if not user.abs_user_id:
            raise ValueError("尚未创建账号")
        try:
            return await operation(user.abs_user_id)
        except AudiobookshelfNotFoundError:
            new_abs_user_id = await self._relink_abs_user_id(session, user)
            if new_abs_user_id is None:
                raise
            return await operation(new_abs_user_id)

    async def _relink_abs_user_id(self, session, user: TgUser) -> str | None:
        username = (user.abs_username or "").strip()
        if not username:
            return None
        matched_user = await self._find_abs_user_by_username(username)
        if matched_user is None:
            return None
        new_abs_user_id = str(matched_user.get("id") or "").strip()
        if not new_abs_user_id:
            raise AudiobookshelfError("Audiobookshelf 用户响应缺少用户 ID")
        old_abs_user_id = user.abs_user_id
        user.abs_user_id = new_abs_user_id
        user.abs_username = str(matched_user.get("username") or username)
        await session.flush()
        logger.info(
            "ABS 用户 ID 已按用户名重新关联：tg=%s username=%s old=%s new=%s",
            user.telegram_id,
            user.abs_username,
            old_abs_user_id,
            new_abs_user_id,
        )
        return new_abs_user_id

    async def get_total_book_count(self) -> int | None:
        """Return total audiobook count across all libraries, or None on error."""
        try:
            libraries = await self.abs_client.get_libraries()
            total = 0
            for lib in libraries:
                lib_id = lib.get("id")
                if not lib_id:
                    continue
                stats = await self.abs_client.get_library_stats(lib_id)
                total += stats.get("totalItems", 0)
            return total
        except AudiobookshelfError:
            return None

    async def sync_users_to_abs(self) -> SyncResult:
        """
        遍历所有有 abs_user_id 的用户：
        - ABS 中不存在 → 重建账号，记入 recreated
        - ABS 中存在   → 根据 is_disabled 同步激活/禁用状态，记入 synced_count
        - 任何异常      → 记入 failed_count，继续下一条
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(TgUser).where(TgUser.abs_user_id.isnot(None))
            )
            users = list(result.scalars().all())

        synced_count = 0
        recreated: list[tuple[int, str, str]] = []
        failed_count = 0

        for user in users:
            try:
                try:
                    await self.abs_client.get_user(user.abs_user_id)
                    # 用户存在，同步激活状态
                    if user.is_disabled:
                        await self.abs_client.disable_user(user.abs_user_id)
                    else:
                        await self.abs_client.restore_user(user.abs_user_id)
                    synced_count += 1
                except AudiobookshelfNotFoundError:
                    username = user.abs_username or f"user_{user.telegram_id}"
                    matched_user = await self._find_abs_user_by_username(username)
                    if matched_user is not None:
                        new_abs_user_id = str(matched_user.get("id") or "").strip()
                        if not new_abs_user_id:
                            raise AudiobookshelfError("Audiobookshelf 用户响应缺少用户 ID")
                        if user.is_disabled:
                            await self.abs_client.disable_user(new_abs_user_id)
                        else:
                            await self.abs_client.restore_user(new_abs_user_id)
                        await self._update_abs_account_mapping(
                            user.telegram_id,
                            abs_user_id=new_abs_user_id,
                            abs_username=str(matched_user.get("username") or username),
                        )
                        synced_count += 1
                        continue

                    # 用户不存在，重建并把新 ABS ID 写回 bot DB。
                    new_password = generate_password()
                    created_user = await self.abs_client.create_user(username, new_password)
                    new_abs_user_id = str(created_user.get("id") or "").strip()
                    if not new_abs_user_id:
                        raise AudiobookshelfError("Audiobookshelf 创建用户响应缺少用户 ID")
                    await self._update_abs_account_mapping(
                        user.telegram_id,
                        abs_user_id=new_abs_user_id,
                        abs_username=str(created_user.get("username") or username),
                        abs_password=new_password,
                    )
                    recreated.append((user.telegram_id, username, new_password))
            except AudiobookshelfError as exc:
                logger.warning("sync_users_to_abs: 用户 %s 同步失败：%s", user.abs_user_id, exc)
                failed_count += 1

        return SyncResult(
            synced_count=synced_count,
            recreated=recreated,
            failed_count=failed_count,
        )

    async def _find_abs_user_by_username(self, username: str) -> dict | None:
        for abs_user in await self.abs_client.list_users():
            if str(abs_user.get("username") or "") == username:
                return abs_user
        return None

    async def _update_abs_account_mapping(
        self,
        telegram_id: int,
        *,
        abs_user_id: str,
        abs_username: str,
        abs_password: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                db_user = await self._get_user_or_fail(session, telegram_id)
                db_user.abs_user_id = abs_user_id
                db_user.abs_username = abs_username
                if abs_password is not None:
                    db_user.abs_password = abs_password


def _as_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _extend_from(current: datetime | None, days: int, now: datetime) -> datetime:
    base = max_datetime(current, now) or now
    return base + timedelta(days=days)


def max_date(left: date | None, right: date | None) -> date | None:
    values = [value for value in (left, right) if value is not None]
    return max(values) if values else None


def _normalize_user_datetimes(user: TgUser) -> TgUser:
    user.expires_at = ensure_utc(user.expires_at)
    user.last_seen_at = ensure_utc(user.last_seen_at)
    user.last_played_at = ensure_utc(user.last_played_at)
    user.disabled_at = ensure_utc(user.disabled_at)
    return user


def _expiration_user_result(
    user: TgUser,
    *,
    points_spent: int = 0,
    disabled_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> ExpirationUserResult:
    return ExpirationUserResult(
        telegram_id=user.telegram_id,
        abs_user_id=user.abs_user_id or "",
        abs_username=user.abs_username,
        expires_at=ensure_utc(user.expires_at),
        points_spent=points_spent,
        disabled_at=ensure_utc(disabled_at or user.disabled_at),
        deleted_at=ensure_utc(deleted_at),
    )


def _snapshot_rebind_request(request: RebindRequest) -> RebindRequestSnapshot:
    values = request.__dict__
    snapshot = RebindRequestSnapshot(
        id=request.id,
        requester_telegram_id=request.requester_telegram_id,
        abs_user_id=request.abs_user_id,
        abs_username=request.abs_username,
        current_telegram_id=request.current_telegram_id,
        status=request.status,
        review_chat_id=request.review_chat_id,
        review_message_id=request.review_message_id,
        reviewed_by=request.reviewed_by,
        reviewed_at=ensure_utc(values.get("reviewed_at")),
        created_at=ensure_utc(values.get("created_at")),
    )
    return snapshot
