"""Разговор про файлы: принять выписку, спросить счёт, показать inbox."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from finance.bot import keys, texts
from finance.bot.engine import Engine
from finance.bot.settings import MAX_BYTES, SUFFIXES
from finance.inbox import InboxError, Row, plain_name

log = logging.getLogger(__name__)


class Choice(CallbackData, prefix="acc"):
    """Кнопка «этот файл — такому-то счёту».

    В обоих полях ключи, а не имена: в callback_data 64 байта на всё, и одно
    только имя счёта их почти выбирает. Как ключи превращаются обратно —
    в finance/bot/keys.py.
    """

    file: str
    account: str


class Statements:
    """Хендлеры про выписки, собранные вокруг одного движка."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.router = Router(name="statements")
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.start, Command("help"))
        self.router.message.register(self.listing, Command("inbox"))
        self.router.message.register(self.document, F.document)
        self.router.message.register(self.photo, F.photo)
        self.router.callback_query.register(self.choose, Choice.filter())

    async def start(self, message: Message) -> None:
        """Что бот умеет."""
        await message.answer(texts.START)

    async def photo(self, message: Message) -> None:
        """Выписку иногда шлют скриншотом — объясняем, почему так нельзя."""
        await message.answer(texts.NOT_A_FILE)

    async def listing(self, message: Message) -> None:
        """Что лежит в inbox и чего ему не хватает."""
        rows = await self.engine.rows()
        if not rows:
            await message.answer(texts.EMPTY_INBOX)
            return

        await message.answer(texts.listing(rows))
        # У каждого неопределившегося файла своя клавиатура: свести их в одну
        # нельзя — кнопка обязана нести и файл, и счёт разом.
        for row in rows:
            if row.choices:
                await message.answer(
                    f"Чей это файл — {row.name}?",
                    reply_markup=await self._choices(row),
                )

    async def document(self, message: Message) -> None:
        """Принять присланный файл."""
        document = message.document
        if document is None:  # F.document уже отсеял, но сузим тип
            return

        name = plain_name(document.file_name or "statement")
        if not name.lower().endswith(SUFFIXES):
            await message.answer(texts.WRONG_SUFFIX)
            return
        # Размер проверяется ДО скачивания: качать двадцать мегабайт, чтобы
        # потом отказаться, — трата и времени, и трафика машины.
        if (document.file_size or 0) > MAX_BYTES:
            await message.answer(texts.TOO_BIG.format(limit=MAX_BYTES // (1024 * 1024)))
            return

        data = await self._download(message, document.file_id)
        if data is None:
            return

        try:
            said = await self.engine.take(name, data)
        except InboxError as error:
            await message.answer(texts.statement(str(error)))
            return

        await message.answer(texts.statement(said), reply_markup=await self._maybe_ask(name))

    async def choose(self, callback: CallbackQuery, callback_data: Choice) -> None:
        """Счёт выбран кнопкой."""
        await callback.answer()
        if callback_data.account == keys.NOTHING:
            await self._replace(callback, texts.SKIPPED)
            return

        try:
            said = await self.engine.assign(callback_data.file, callback_data.account)
        except InboxError as error:
            said = str(error)
        await self._replace(callback, texts.statement(said))

    # ─────────────────────────── внутреннее ───────────────────────────

    async def _download(self, message: Message, file_id: str) -> bytes | None:
        """Скачать вложение. None — не вышло, человеку уже сказали."""
        if message.bot is None:
            return None
        buffer = await message.bot.download(file_id)
        if buffer is None:
            await message.answer(texts.STALE)
            return None
        return buffer.read()

    async def _maybe_ask(self, name: str) -> InlineKeyboardMarkup | None:
        """Клавиатура, если после раскладки счёт всё ещё не определился."""
        for row in await self.engine.rows():
            if row.name.endswith(name) and row.choices:
                return await self._choices(row)
        return None

    async def _choices(self, row: Row) -> InlineKeyboardMarkup:
        """Кнопка на каждого кандидата плюс «пропустить»."""
        file_key = keys.of(row.name)
        buttons = [
            [
                InlineKeyboardButton(
                    text=choice.split(":")[-1],
                    callback_data=Choice(file=file_key, account=keys.of(choice)).pack(),
                )
            ]
            for choice in row.choices
        ]
        buttons.append(
            [
                InlineKeyboardButton(
                    text="пропустить",
                    callback_data=Choice(file=file_key, account=keys.NOTHING).pack(),
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    async def _replace(callback: CallbackQuery, text: str) -> None:
        """Переписать сообщение с кнопками итогом: нажать второй раз нельзя."""
        if isinstance(callback.message, Message):
            await callback.message.edit_text(text, reply_markup=None)
