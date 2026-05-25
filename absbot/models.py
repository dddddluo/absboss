from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy import UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class RedeemCodeType(str, Enum):
    REGISTRATION = "registration"
    RENEWAL = "renewal"
    WHITELIST = "whitelist"


class RebindRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RegistrationQueueStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class TgUser(Base):
    __tablename__ = "tg_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    abs_user_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    abs_username: Mapped[str | None] = mapped_column(String(128), unique=True)
    abs_password: Mapped[str | None] = mapped_column(String(128))
    registration_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_whitelisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_checkin_date: Mapped[date | None] = mapped_column(Date)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    renewal_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tg_display_name: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    redemptions: Mapped[list["RedeemCodeUse"]] = relationship(back_populates="user")


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RegistrationQueue(Base):
    __tablename__ = "registration_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    abs_username: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[RegistrationQueueStatus] = mapped_column(
        SAEnum(RegistrationQueueStatus, native_enum=False),
        nullable=False,
        default=RegistrationQueueStatus.PENDING,
        index=True,
    )
    position: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    result_password: Mapped[str | None] = mapped_column(String(128))
    result_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RedeemCode(Base):
    __tablename__ = "redeem_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    code_type: Mapped[RedeemCodeType] = mapped_column(
        SAEnum(RedeemCodeType, native_enum=False), nullable=False
    )
    days: Mapped[int | None] = mapped_column(Integer)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    uses: Mapped[list["RedeemCodeUse"]] = relationship(back_populates="code")


class RedeemCodeUse(Base):
    __tablename__ = "redeem_code_uses"
    __table_args__ = (UniqueConstraint("code_id", "telegram_id", name="uq_code_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_id: Mapped[int] = mapped_column(ForeignKey("redeem_codes.id"), nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tg_users.telegram_id"))
    used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    code: Mapped[RedeemCode] = relationship(back_populates="uses")
    user: Mapped[TgUser] = relationship(back_populates="redemptions")


class RebindRequest(Base):
    __tablename__ = "rebind_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requester_telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tg_users.telegram_id"), nullable=False, index=True
    )
    abs_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    abs_username: Mapped[str] = mapped_column(String(128), nullable=False)
    current_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[RebindRequestStatus] = mapped_column(
        SAEnum(RebindRequestStatus, native_enum=False),
        nullable=False,
        default=RebindRequestStatus.PENDING,
    )
    review_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    review_message_id: Mapped[int | None] = mapped_column(Integer)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    target_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ListeningSession(Base):
    __tablename__ = "listening_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    abs_session_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    abs_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    library_item_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    display_title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
