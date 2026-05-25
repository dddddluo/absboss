from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_TABLE_ORDER = [
    "bot_settings",
    "tg_users",
    "registration_queue",
    "redeem_codes",
    "redeem_code_uses",
    "rebind_requests",
    "audit_logs",
]

_BACKUP_GLOB = "backup_*.sql"


def _escape_value(v: object) -> str:
    """将 Python 值转义为 MySQL SQL 字面量。"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (datetime, date)):
        return f"'{v.isoformat()}'"
    if isinstance(v, bytes):
        return f"X'{v.hex()}'"
    s = str(v)
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "\\'")
    s = s.replace("\n", "\\n")
    s = s.replace("\r", "\\r")
    return f"'{s}'"


def list_local_backups(backup_dir: str) -> list[Path]:
    """返回备份文件列表，按文件名倒序（最新在前）。目录不存在时自动创建。"""
    path = Path(backup_dir)
    path.mkdir(parents=True, exist_ok=True)
    files = sorted(path.glob(_BACKUP_GLOB), key=lambda p: p.name, reverse=True)
    return list(files)


def cleanup_old_backups(backup_dir: str, keep_count: int) -> int:
    """删除超出 keep_count 的最旧备份文件，返回删除数量。"""
    backups = list_local_backups(backup_dir)
    to_delete = backups[keep_count:]
    for p in to_delete:
        p.unlink(missing_ok=True)
    return len(to_delete)


async def dump_database(engine: AsyncEngine, backup_dir: str) -> Path:
    """
    将数据库所有表导出为 SQL 文件。返回生成的文件路径。
    格式：MySQL 兼容 SQL，每条语句独占一行，字符串特殊字符已转义。
    """
    backup_path = Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    filename = f"backup_{now.strftime('%Y%m%d_%H%M%S')}.sql"
    file_path = backup_path / filename

    lines: list[str] = [
        "-- AudiobookshelfBot database backup",
        f"-- Generated: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        "SET FOREIGN_KEY_CHECKS=0;",
        "SET NAMES utf8mb4;",
    ]

    async with engine.connect() as conn:
        for table_name in _TABLE_ORDER:
            lines.append(f"DELETE FROM `{table_name}`;")
            result = await conn.execute(text(f"SELECT * FROM `{table_name}`"))
            cols = list(result.keys())
            rows = result.fetchall()
            if rows:
                col_list = ", ".join(f"`{c}`" for c in cols)
                value_rows = [
                    "(" + ", ".join(_escape_value(v) for v in row) + ")"
                    for row in rows
                ]
                lines.append(
                    f"INSERT INTO `{table_name}` ({col_list}) VALUES "
                    + ", ".join(value_rows)
                    + ";"
                )

    lines.append("SET FOREIGN_KEY_CHECKS=1;")

    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("数据库备份完成：%s", file_path)
    return file_path


async def restore_database(engine: AsyncEngine, backup_path: Path) -> None:
    """
    从备份文件恢复数据库。在同一事务中执行所有语句。
    跳过注释行和空行；MySQL 专属语句（SET FOREIGN_KEY_CHECKS 等）
    在非 MySQL 引擎上静默跳过。
    """
    sql_text = backup_path.read_text(encoding="utf-8")

    async with engine.begin() as conn:
        for raw_line in sql_text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("--"):
                continue
            stmt = line.rstrip(";").strip()
            if not stmt:
                continue
            try:
                await conn.execute(text(stmt))
            except Exception as exc:
                # MySQL 专属语句在 SQLite 等引擎上会失败，忽略
                if any(
                    kw in stmt.upper()
                    for kw in ("FOREIGN_KEY_CHECKS", "NAMES UTF8MB4")
                ):
                    continue
                raise exc

    logger.info("数据库恢复完成：%s", backup_path)
