from __future__ import annotations

from absbot.config import Settings


def _make_settings(admin_ids: set[int] = frozenset(), owner_id: int | None = None) -> Settings:
    return Settings(
        bot_token="x",
        admin_tg_ids=set(admin_ids),
        mysql_dsn="x",
        abs_base_url="x",
        abs_api_token="x",
        owner_tg_id=owner_id,
    )


class TestIsAdmin:
    def test_admin_in_admin_ids(self):
        s = _make_settings(admin_ids={111})
        assert s.is_admin(111) is True

    def test_non_admin(self):
        s = _make_settings(admin_ids={111})
        assert s.is_admin(222) is False

    def test_none_telegram_id(self):
        s = _make_settings(admin_ids={111})
        assert s.is_admin(None) is False

    def test_owner_is_also_admin(self):
        s = _make_settings(owner_id=999)
        assert s.is_admin(999) is True

    def test_owner_not_in_admin_ids_still_is_admin(self):
        s = _make_settings(admin_ids={111}, owner_id=999)
        assert s.is_admin(999) is True

    def test_owner_id_none_does_not_grant_admin(self):
        s = _make_settings(admin_ids=set(), owner_id=None)
        assert s.is_admin(999) is False


class TestIsOwner:
    def test_owner(self):
        s = _make_settings(owner_id=999)
        assert s.is_owner(999) is True

    def test_non_owner(self):
        s = _make_settings(owner_id=999)
        assert s.is_owner(111) is False

    def test_none_telegram_id(self):
        s = _make_settings(owner_id=999)
        assert s.is_owner(None) is False

    def test_no_owner_configured(self):
        s = _make_settings(owner_id=None)
        assert s.is_owner(999) is False
