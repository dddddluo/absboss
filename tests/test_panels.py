from pathlib import Path

import pytest

from absbot.panels import send_panel


class FakeMessage:
    def __init__(self, fail_photo=False):
        self.fail_photo = fail_photo
        self.photo_calls = []
        self.text_calls = []

    async def answer_photo(self, photo, *, caption, reply_markup=None, parse_mode=None):
        if self.fail_photo:
            raise RuntimeError("photo failed")
        self.photo_calls.append((photo, caption, reply_markup, parse_mode))

    async def answer(self, text, *, reply_markup=None, parse_mode=None):
        self.text_calls.append((text, reply_markup, parse_mode))


@pytest.mark.asyncio
async def test_send_panel_uses_https_url_directly_with_caption(tmp_path):
    message = FakeMessage()

    await send_panel(
        message,
        "caption text",
        reply_markup="keyboard",
        panel_photo_path="https://example.com/panel.jpg",
        default_photo_path=tmp_path / "default.png",
    )

    assert message.photo_calls == [("https://example.com/panel.jpg", "caption text", "keyboard", "HTML")]
    assert message.text_calls == []


@pytest.mark.asyncio
async def test_send_panel_uses_configured_photo_with_caption(tmp_path):
    photo = tmp_path / "configured.png"
    photo.write_bytes(b"png")
    message = FakeMessage()

    await send_panel(
        message,
        "caption text",
        reply_markup="keyboard",
        panel_photo_path=photo,
        default_photo_path=tmp_path / "default.png",
    )

    assert message.photo_calls
    assert message.photo_calls[0][1] == "caption text"
    assert message.photo_calls[0][2] == "keyboard"
    assert message.photo_calls[0][3] == "HTML"
    assert message.text_calls == []


@pytest.mark.asyncio
async def test_send_panel_falls_back_to_default_photo_when_config_missing(tmp_path, caplog):
    default_photo = tmp_path / "default.png"
    default_photo.write_bytes(b"png")
    message = FakeMessage()

    await send_panel(
        message,
        "caption text",
        reply_markup=None,
        panel_photo_path=tmp_path / "missing.png",
        default_photo_path=default_photo,
    )

    assert message.photo_calls
    assert "configured panel photo is unavailable" in caplog.text or "配置的面板图片不可用" in caplog.text


@pytest.mark.asyncio
async def test_send_panel_falls_back_to_text_when_all_photos_fail(tmp_path, caplog):
    default_photo = tmp_path / "default.png"
    default_photo.write_bytes(b"png")
    message = FakeMessage(fail_photo=True)

    await send_panel(
        message,
        "caption text",
        reply_markup="keyboard",
        panel_photo_path=Path("missing.png"),
        default_photo_path=default_photo,
    )

    assert message.photo_calls == []
    assert message.text_calls == [("caption text", "keyboard", "HTML")]
    assert "panel photo send failed" in caplog.text or "面板图片发送失败" in caplog.text


@pytest.mark.asyncio
async def test_send_panel_allows_plain_text_parse_mode(tmp_path):
    message = FakeMessage()

    await send_panel(
        message,
        "caption text",
        panel_photo_path="https://example.com/panel.jpg",
        default_photo_path=tmp_path / "default.png",
        parse_mode=None,
    )

    assert message.photo_calls == [("https://example.com/panel.jpg", "caption text", None, None)]
