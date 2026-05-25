import logging
from logging.handlers import RotatingFileHandler

from absbot.config import Settings


def set_required_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_TG_IDS", "1")
    monkeypatch.setenv("MYSQL_DSN", "mysql+aiomysql://u:p@localhost/db")
    monkeypatch.setenv("ABS_BASE_URL", "https://abs.example.com")
    monkeypatch.setenv("ABS_API_TOKEN", "abs-token")


def test_logging_settings_defaults(monkeypatch):
    set_required_env(monkeypatch)

    settings = Settings.from_env(env_file="missing.env")

    assert settings.log_file == "logs/app.log"
    assert settings.log_max_bytes == 10_485_760
    assert settings.log_backup_count == 5


def test_logging_settings_env_overrides(monkeypatch):
    set_required_env(monkeypatch)
    monkeypatch.setenv("LOG_FILE", "runtime/bot.log")
    monkeypatch.setenv("LOG_MAX_BYTES", "2048")
    monkeypatch.setenv("LOG_BACKUP_COUNT", "3")

    settings = Settings.from_env(env_file="missing.env")

    assert settings.log_file == "runtime/bot.log"
    assert settings.log_max_bytes == 2048
    assert settings.log_backup_count == 3


def test_configure_logging_adds_console_and_rotating_file_handlers(tmp_path):
    from absbot.logging_config import configure_logging

    settings = Settings(
        bot_token="token",
        admin_tg_ids={1},
        mysql_dsn="mysql+aiomysql://u:p@localhost/db",
        abs_base_url="https://abs.example.com",
        abs_api_token="abs-token",
        log_level="DEBUG",
        log_file=str(tmp_path / "nested" / "app.log"),
        log_max_bytes=1234,
        log_backup_count=2,
    )

    configure_logging(settings)

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, RotatingFileHandler)
        for handler in root.handlers
    )
    file_handlers = [handler for handler in root.handlers if isinstance(handler, RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == 1234
    assert file_handlers[0].backupCount == 2
    assert (tmp_path / "nested").is_dir()

    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)


def test_configure_logging_writes_startup_record(tmp_path):
    from absbot.logging_config import configure_logging

    log_file = tmp_path / "app.log"
    settings = Settings(
        bot_token="token",
        admin_tg_ids={1},
        mysql_dsn="mysql+aiomysql://u:p@localhost/db",
        abs_base_url="https://abs.example.com",
        abs_api_token="abs-token",
        log_file=str(log_file),
    )

    configure_logging(settings)

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "Logging configured" in log_file.read_text(encoding="utf-8")

    root = logging.getLogger()
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)
