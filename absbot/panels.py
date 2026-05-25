from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from aiogram.types import FSInputFile


logger = logging.getLogger(__name__)
DEFAULT_PANEL_PHOTO_PATH = Path(__file__).resolve().parents[1] / "assets" / "default_panel.png"
DEFAULT_PARSE_MODE = "HTML"


async def send_panel(
    message,
    caption: str,
    *,
    reply_markup=None,
    panel_photo_path: str | Path | None = None,
    default_photo_path: str | Path = DEFAULT_PANEL_PHOTO_PATH,
    parse_mode: str | None = DEFAULT_PARSE_MODE,
) -> None:
    for index, candidate in enumerate(_candidate_photos(panel_photo_path, default_photo_path)):
        if isinstance(candidate, Path):
            if not candidate.is_file():
                if index == 0:
                    logger.warning("配置的面板图片不可用：%s", candidate)
                else:
                    logger.warning("默认面板图片不可用：%s", candidate)
                continue
            photo = FSInputFile(candidate)
            label = str(candidate)
        else:
            photo = candidate
            label = candidate

        try:
            await message.answer_photo(photo, caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except Exception as exc:  # Telegram errors vary by transport and file backend.
            logger.warning("面板图片发送失败（%s）：%s", label, exc)

    await message.answer(caption, reply_markup=reply_markup, parse_mode=parse_mode)


async def replace_panel(
    callback,
    caption: str,
    *,
    reply_markup=None,
    panel_photo_path: str | Path | None = None,
    default_photo_path: str | Path = DEFAULT_PANEL_PHOTO_PATH,
    parse_mode: str | None = DEFAULT_PARSE_MODE,
) -> None:
    message = callback.message
    if message is None:
        return
    if getattr(message, "photo", None):
        try:
            await message.edit_caption(caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("面板说明文字编辑失败：%s", exc)
    try:
        await message.delete()
    except Exception as exc:
        logger.warning("旧面板删除失败：%s", exc)
        try:
            await message.edit_text(caption, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except Exception as edit_exc:
            logger.warning("面板文字编辑失败：%s", edit_exc)
    await send_panel(
        message,
        caption,
        reply_markup=reply_markup,
        panel_photo_path=panel_photo_path,
        default_photo_path=default_photo_path,
        parse_mode=parse_mode,
    )


def _candidate_photos(
    panel_photo_path: str | Path | None,
    default_photo_path: str | Path,
) -> list[str | Path]:
    candidates: list[str | Path] = []
    if panel_photo_path:
        configured = str(panel_photo_path).strip()
        if _is_http_url(configured):
            candidates.append(configured)
        else:
            candidates.append(Path(configured))
    default_path = Path(default_photo_path)
    if default_path not in candidates:
        candidates.append(default_path)
    return candidates


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

