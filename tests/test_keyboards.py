from __future__ import annotations

from absbot.keyboards import backup_confirm_keyboard, backup_panel_keyboard


def test_backup_panel_keyboard_contains_backup_buttons() -> None:
    keyboard = backup_panel_keyboard(
        backups=["backup_20260522_050000.sql", "backup_20260521_050000.sql"],
        owner_id=999,
    )
    all_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert "admin:backup:restore:backup_20260522_050000.sql" in all_data
    assert "admin:backup:restore:backup_20260521_050000.sql" in all_data
    assert "admin:backup:run" in all_data
    assert "admin:home" in all_data


def test_backup_panel_keyboard_empty_backups() -> None:
    keyboard = backup_panel_keyboard(backups=[], owner_id=999)
    all_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert "admin:backup:run" in all_data
    assert "admin:home" in all_data
    # 无备份时不应有 restore 按钮
    assert not any("restore" in (d or "") for d in all_data)


def test_backup_confirm_keyboard_has_confirm_and_cancel() -> None:
    keyboard = backup_confirm_keyboard("backup_20260522_050000.sql")
    all_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert "admin:backup:do_restore:backup_20260522_050000.sql" in all_data
    assert "admin:backup" in all_data
