from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from absbot.abs_client import AudiobookshelfClient
from absbot.commands import setup_bot_commands
from absbot.config import Settings
from absbot.db import create_engine, create_session_factory, run_migrations
from absbot.handlers import router
from absbot.leaderboard import LeaderboardService
from absbot.logging_config import configure_logging
from absbot.scheduler import create_scheduler, run_registration_queue_worker
from absbot.service import MembershipService


logger = logging.getLogger(__name__)


async def main() -> None:
    settings = Settings.from_env()
    await run_migrations(settings.mysql_dsn)
    configure_logging(settings)
    engine = create_engine(settings.mysql_dsn)
    session_factory = create_session_factory(engine)

    async with AudiobookshelfClient(settings.abs_base_url, settings.abs_api_token) as abs_client:
        service = MembershipService(
            session_factory,
            abs_client,
            registration_queue_delay_seconds=settings.registration_queue_delay_seconds,
        )
        await service.reset_stuck_queue_items()
        system_settings = await service.get_system_settings()
        leaderboard_service = LeaderboardService(session_factory, abs_client)
        bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        scheduler = create_scheduler(
            service, bot, engine, settings,
            timezone=settings.timezone,
            leaderboard_service=leaderboard_service,
        )
        registration_queue_task: asyncio.Task | None = None
        try:
            await setup_bot_commands(bot, settings, main_group_chat_id=system_settings.main_group_chat_id)
            dp = Dispatcher()
            dp.include_router(router)
            scheduler.start()
            registration_queue_task = asyncio.create_task(run_registration_queue_worker(service, bot))
            await dp.start_polling(
                bot, service=service, settings=settings, scheduler=scheduler, engine=engine,
                leaderboard_service=leaderboard_service,
            )
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            if registration_queue_task is not None:
                registration_queue_task.cancel()
                try:
                    await registration_queue_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("注册队列后台任务退出异常")
            await bot.session.close()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
