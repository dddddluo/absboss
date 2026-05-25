from pathlib import Path

from absbot import db


def test_create_engine_pre_pings_pooled_mysql_connections(monkeypatch):
    calls = []

    def fake_create_async_engine(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return object()

    monkeypatch.setattr(db, "create_async_engine", fake_create_async_engine)

    engine = db.create_engine("mysql+aiomysql://user:pass@db/app")

    assert engine is not None
    assert calls == [
        (
            "mysql+aiomysql://user:pass@db/app",
            {"pool_pre_ping": True},
        )
    ]


async def test_run_migrations_upgrades_alembic_to_head(monkeypatch):
    calls = []

    def fake_upgrade(config, revision):
        calls.append((config, revision))

    monkeypatch.setattr(db.command, "upgrade", fake_upgrade)

    await db.run_migrations("mysql+aiomysql://user:pass@db/app")

    assert len(calls) == 1
    config, revision = calls[0]
    assert revision == "head"
    assert config.config_file_name == str(Path("alembic.ini"))
    assert config.get_main_option("sqlalchemy.url") == "mysql+aiomysql://user:pass@db/app"
