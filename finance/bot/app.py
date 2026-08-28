"""Сборка бота: диспетчер, роутеры, вебхук.

Наружу бот не смотрит: слушает петлю, а до Telegram его пускает Caddy — тот
же, что стоит перед fava. Секрет пути знает только этот модуль, Caddy
проксирует несекретный префикс целиком (почему так — в deploy/Caddyfile).
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from finance.bot.access import Allowlist
from finance.bot.chain import Chain
from finance.bot.engine import Engine
from finance.bot.settings import Settings
from finance.bot.statements import Statements

log = logging.getLogger(__name__)

#: Паузы между попытками поставить вебхук, секунды. Сеть у Telegram может и
#: не ответить, но отказ регистрации — не повод не принимать уже летящий
#: к нам апдейт, поэтому попытки идут фоном.
RETRIES = (0, 2, 5, 15, 60)

COMMANDS = [
    BotCommand(command="inbox", description="что лежит и ждёт разбора"),
    BotCommand(command="import", description="разобрать и перенести в леджер"),
    BotCommand(command="help", description="что я умею"),
]


class BotApp:
    """Бот целиком: и разговор, и то, как его слышно снаружи."""

    def __init__(self, settings: Settings, engine: Engine | None = None) -> None:
        self.settings = settings
        self.engine = engine or Engine()
        self.bot = Bot(
            settings.token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dispatcher = self.build_dispatcher()

    def build_dispatcher(self) -> Dispatcher:
        """Диспетчер со всеми роутерами и проверкой доступа перед ними."""
        dispatcher = Dispatcher()
        # Внешней middleware: проверка «свой ли» обязана стоять до всех
        # фильтров и накрывать разом сообщения и нажатия кнопок.
        dispatcher.update.outer_middleware(Allowlist(self.settings.allowed))
        dispatcher.include_router(Statements(self.engine).router)
        dispatcher.include_router(Chain(self.engine).router)
        return dispatcher

    def web_app(self) -> web.Application:
        """aiohttp-приложение с одним маршрутом — вебхуком."""
        app = web.Application()
        SimpleRequestHandler(
            dispatcher=self.dispatcher,
            bot=self.bot,
            # Явно, хотя это и умолчание: ответить Telegram нужно сразу, и
            # менять такое молчаливым обновлением библиотеки не хочется.
            handle_in_background=True,
            secret_token=self.settings.secret,
        ).register(app, path=self.settings.route)
        setup_application(app, self.dispatcher, bot=self.bot)
        app.on_startup.append(self._announce)
        return app

    async def _announce(self, _app: web.Application) -> None:
        """Назваться Telegram — фоном.

        Именно фоном: aiohttp выполняет startup-обработчики ДО того, как
        начинает принимать соединения, и медленный setWebhook задержал бы
        ровно тот запрос, которым нас разбудили.
        """
        asyncio.create_task(self._register())  # noqa: RUF006

    async def _register(self) -> None:
        """Поставить вебхук, не сдаваясь с первого раза.

        Безусловно, без сверки с getWebhookInfo: секрет заголовка тот не
        возвращает, так что полной проверки всё равно не построить. setWebhook
        идемпотентен и очередь не сбрасывает, а лишний вызов на перезапуске
        дешевле, чем неполная проверка. Заодно это чинит вебхук, если его
        увёл тестовый бот.
        """
        await self.bot.set_my_commands(COMMANDS)
        for pause in RETRIES:
            await asyncio.sleep(pause)
            try:
                await self.bot.set_webhook(
                    url=self.settings.url,
                    secret_token=self.settings.secret,
                    allowed_updates=["message", "callback_query"],
                    # Одна маленькая машина и один замок на импорт:
                    # последовательная доставка тут и нужна.
                    max_connections=1,
                    # Сбрасывать очередь нельзя: пока машина спала, в ней
                    # лежит ровно то, ради чего её и будят.
                    drop_pending_updates=False,
                )
                log.info("вебхук поставлен")
                return
            except TelegramAPIError as error:
                log.warning("setWebhook не удался: %s", error)
        log.error("вебхук поставить так и не вышло")

    def run(self) -> None:
        """Поднять бота — вебхуком или опросом."""
        if self.settings.polling:
            asyncio.run(self._poll())
            return
        web.run_app(
            self.web_app(),
            host=self.settings.host,
            port=self.settings.port,
            print=None,
        )

    async def _poll(self) -> None:
        """Опрос вместо вебхука — для работы над разговором без адреса.

        У бота ровно один вебхук, и опрос с ним несовместим, поэтому вебхук
        сначала снимается. Для локальной работы нужен ОТДЕЛЬНЫЙ бот: сняв
        вебхук у боевого, вы молча его сломаете.
        """
        await self.bot.delete_webhook(drop_pending_updates=False)
        await self.bot.set_my_commands(COMMANDS)
        await self.dispatcher.start_polling(self.bot)

    @classmethod
    def build(cls) -> BotApp | None:
        """Собрать по окружению. None — бота не просили."""
        settings = Settings.load()
        return cls(settings) if settings else None
