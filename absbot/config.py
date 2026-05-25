from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_int_set(value: str | None) -> set[int]:
    if not value:
        return set()
    result: set[int] = set()
    for item in value.replace(";", ",").split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return result


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value.strip())


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_tg_ids: set[int]
    mysql_dsn: str
    abs_base_url: str
    abs_api_token: str
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    log_file: str = "logs/app.log"
    log_max_bytes: int = 10_485_760
    log_backup_count: int = 5
    owner_tg_id: int | None = None
    backup_dir: str = "backups"
    backup_keep_count: int = 7
    registration_queue_delay_seconds: float = 2.0

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        load_dotenv(env_file)
        return cls(
            bot_token=_required("BOT_TOKEN"),
            admin_tg_ids=_parse_int_set(os.getenv("ADMIN_TG_IDS")),
            mysql_dsn=_required("MYSQL_DSN"),
            abs_base_url=_required("ABS_BASE_URL"),
            abs_api_token=_required("ABS_API_TOKEN"),
            timezone=os.getenv("BOT_TIMEZONE", "Asia/Shanghai"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_file=os.getenv("LOG_FILE", "logs/app.log"),
            log_max_bytes=int(os.getenv("LOG_MAX_BYTES", "10485760")),
            log_backup_count=int(os.getenv("LOG_BACKUP_COUNT", "5")),
            owner_tg_id=_optional_int(os.getenv("OWNER_TG_ID")),
            backup_dir=os.getenv("BACKUP_DIR", "backups"),
            backup_keep_count=int(os.getenv("BACKUP_KEEP_COUNT", "7")),
            registration_queue_delay_seconds=float(os.getenv("REGISTRATION_QUEUE_DELAY_SECONDS", "2.0")),
        )

    def is_admin(self, telegram_id: int | None) -> bool:
        return telegram_id is not None and (telegram_id in self.admin_tg_ids or self.owner_tg_id == telegram_id)

    def is_owner(self, telegram_id: int | None) -> bool:
        return telegram_id is not None and self.owner_tg_id == telegram_id


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value
