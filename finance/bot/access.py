"""Кого бот вообще слушает.

Список chat id, и ничего умнее: бот пишет в семейный леджер, и круг тех, кому
это позволено, известен заранее и не меняется.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

log = logging.getLogger(__name__)

#: Что слышит чужой. Одна фраза без подробностей: объяснять постороннему
#: устройство домашнего леджера незачем.
STRANGER = "Это личный бот семьи. Здесь для вас ничего нет."

#: Что слышит свой, пока список пуст. Так и узнают свой chat id: другого
#: способа нет, а отправлять человека к чужому боту ради этого не хочется.
UNCONFIGURED = (
    "Бот ещё не настроен. Ваш chat id: {chat}\n"
    "Впишите его в TELEGRAM_ALLOWED и перезапустите."
)


class Allowlist(BaseMiddleware):
    """Проверка «свой ли» — раньше всех остальных.

    Внешней middleware на `dp.update`, а не фильтром на роутерах: так она
    стоит до всех фильтров и накрывает разом и сообщения, и нажатия кнопок.
    Повесить её на новый роутер невозможно забыть — вешать некуда.

    Пустой список никого не пускает. Это не запрет на запуск: бот с пустым
    списком нужен ровно затем, чтобы узнать свой chat id, и он его называет.
    """

    def __init__(self, allowed: frozenset[int]) -> None:
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = self._chat(event)
        if chat is not None and chat in self.allowed:
            return await handler(event, data)

        log.warning("отказано: chat_id=%s", chat)
        await self._refuse(event, chat)
        return None

    @staticmethod
    def _chat(event: TelegramObject) -> int | None:
        """Чей это апдейт. None — из тех, что мы и не обрабатываем."""
        if not isinstance(event, Update):
            return None
        if event.message is not None:
            return event.message.chat.id
        if event.callback_query is not None and event.callback_query.message is not None:
            return event.callback_query.message.chat.id
        return None

    async def _refuse(self, event: TelegramObject, chat: int | None) -> None:
        """Отказать вслух. Молчать хуже: человек не поймёт, бот ли сломался."""
        if not isinstance(event, Update) or chat is None:
            return
        text = UNCONFIGURED.format(chat=chat) if not self.allowed else STRANGER
        if event.message is not None:
            await event.message.answer(text)
        elif event.callback_query is not None:
            await event.callback_query.answer(text, show_alert=True)
