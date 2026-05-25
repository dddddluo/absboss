from __future__ import annotations

import html
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile

from sqlalchemy.ext.asyncio import AsyncEngine

from absbot.backup import cleanup_old_backups, dump_database, list_local_backups, restore_database
from absbot.config import Settings
from absbot.keyboards import backup_confirm_keyboard, backup_panel_keyboard
from absbot.service import MembershipService

logger = logging.getLogger(__name__)
router = Router()


def _require_owner(callback: CallbackQuery, settings: Settings) -> bool:
    return settings.is_owner(callback.from_user.id)


async def _edit_backup_message(callback: CallbackQuery, text: str, *, reply_markup=None) -> None:
    message = callback.message
    if message is None:
        return
    if getattr(message, "caption", None) is not None:
        await message.edit_caption(caption=text, reply_markup=reply_markup)
        return
    await message.edit_text(text=text, reply_markup=reply_markup)


async def _send_backup_panel(callback: CallbackQuery, settings: Settings) -> None:
    backups = list_local_backups(settings.backup_dir)
    filenames = [p.name for p in backups]
    keyboard = backup_panel_keyboard(filenames, callback.from_user.id)
    text = (
        f"🗄 <b>备份管理</b>\n\n本地共 {len(filenames)} 份备份"
        if filenames
        else "🗄 <b>备份管理</b>\n\n暂无本地备份"
    )
    await _edit_backup_message(callback, text, reply_markup=keyboard)


@router.callback_query(F.data == "admin:backup")
async def backup_panel(callback: CallbackQuery, settings: Settings) -> None:
    if not _require_owner(callback, settings):
        await callback.answer("没有权限", show_alert=True)
        return
    await callback.answer()
    await _send_backup_panel(callback, settings)


@router.callback_query(F.data == "admin:backup:run")
async def run_manual_backup(
    callback: CallbackQuery, settings: Settings, engine: AsyncEngine
) -> None:
    if not _require_owner(callback, settings):
        await callback.answer("没有权限", show_alert=True)
        return
    await callback.answer("⏳ 正在备份，请稍候…")
    try:
        backup_path = await dump_database(engine, settings.backup_dir)
        cleanup_old_backups(settings.backup_dir, settings.backup_keep_count)
        if callback.bot and settings.owner_tg_id:
            await callback.bot.send_document(
                settings.owner_tg_id,
                FSInputFile(backup_path),
                caption=f"🗄 手动备份\n{backup_path.name}",
            )
    except Exception as exc:
        await callback.answer(f"备份失败：{exc}", show_alert=True)
        return
    await _send_backup_panel(callback, settings)


@router.callback_query(F.data.startswith("admin:backup:restore:"))
async def backup_restore_select(callback: CallbackQuery, settings: Settings) -> None:
    if not _require_owner(callback, settings):
        await callback.answer("没有权限", show_alert=True)
        return
    await callback.answer()
    filename = callback.data.removeprefix("admin:backup:restore:")
    keyboard = backup_confirm_keyboard(filename)
    text = (
        f"⚠️ <b>确认恢复？</b>\n\n"
        f"备份文件：<code>{html.escape(filename)}</code>\n\n"
        f"将覆盖当前数据库并同步至 ABS 服务。\n"
        f"<b>此操作不可逆。</b>"
    )
    await _edit_backup_message(callback, text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("admin:backup:do_restore:"))
async def backup_restore_execute(
    callback: CallbackQuery,
    settings: Settings,
    service: MembershipService,
    engine: AsyncEngine,
) -> None:
    if not _require_owner(callback, settings):
        await callback.answer("没有权限", show_alert=True)
        return
    await callback.answer("⏳ 正在恢复，请稍候…")

    filename = callback.data.removeprefix("admin:backup:do_restore:")
    backup_path = Path(settings.backup_dir) / filename
    if not backup_path.exists():
        await callback.answer(f"备份文件不存在：{filename}", show_alert=True)
        return

    processing_text = f"⏳ <b>正在恢复数据库…</b>\n\n文件：<code>{html.escape(filename)}</code>"
    await _edit_backup_message(callback, processing_text, reply_markup=None)

    try:
        await restore_database(engine, backup_path)
        sync_result = await service.sync_users_to_abs()
    except Exception as exc:
        logger.error("数据库恢复失败：%s", exc)
        await _edit_backup_message(callback, f"❌ <b>恢复失败</b>\n\n{html.escape(str(exc))}")
        return

    for tg_id, username, password in sync_result.recreated:
        try:
            await callback.bot.send_message(
                tg_id,
                f"🔐 您在 Audiobookshelf 的账号已随数据库恢复而重建。\n"
                f"用户名：<code>{html.escape(username)}</code>\n"
                f"新密码：<code>{html.escape(password)}</code>",
            )
        except Exception as exc:
            logger.warning("无法发送重建通知给用户 %s：%s", tg_id, exc)

    summary = (
        f"✅ <b>恢复完成</b>\n\n"
        f"来源：<code>{html.escape(filename)}</code>\n"
        f"ABS 同步：已同步 {sync_result.synced_count} 人，"
        f"重建 {len(sync_result.recreated)} 人，"
        f"失败 {sync_result.failed_count} 人"
    )
    await _edit_backup_message(callback, summary, reply_markup=None)
