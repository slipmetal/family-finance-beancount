"""Команда /import: разобрать inbox и, с согласия, перенести в леджер.

Разбор и перенос разделены тем же вопросом, что и в терминале: сначала видно,
сколько проводок разобрано, сколько сочтено дубликатами и что осталось без
категории, — и только потом решается, переносить ли. В README на этом просмотре
настаивают, и терять его в чате незачем.

Работа уходит в отдельную задачу, а вебхуку отвечается сразу. Telegram ждёт
быстрый ответ и на медленный присылает тот же апдейт заново — а цепочка идёт
десятками секунд, из них заметная часть в сети за курсами валют.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from finance.bot import texts
from finance.bot.engine import Busy, Engine

log = logging.getLogger(__name__)

#: Задачи держатся здесь, пока идут. Без этого сборщик мусора вправе убрать
#: задачу на полпути: asyncio держит на неё лишь слабую ссылку.
RUNNING: set[asyncio.Task] = set()


class Run(CallbackData, prefix="run"):
    """Кнопка под отчётом о разборе."""

    go: bool
    replace: bool


class Chain:
    """Хендлеры про импорт целиком."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.router = Router(name="chain")
        self.router.message.register(self.start, Command("import"))
        self.router.callback_query.register(self.confirm, Run.filter())

    async def start(self, message: Message, command: CommandObject) -> None:
        """Разобрать inbox и показать, что получилось.

        `--replace` нужен после правки rules.yaml: без него переразмеченные
        проводки лягут в леджер вторым экземпляром.
        """
        replace = (command.args or "").strip() == "replace"
        if self.engine.busy:
            await message.answer(texts.BUSY)
            return

        note = await message.answer("Разбираю выписки…")
        try:
            extraction = await self.engine.extract()
        except Busy:
            await note.edit_text(texts.BUSY)
            return

        if extraction.failed:
            await note.edit_text(texts.failure(extraction.name, extraction.output))
            return
        if extraction.empty:
            await note.edit_text(texts.NOTHING_NEW)
            return

        await note.edit_text(
            texts.report(extraction), reply_markup=self._ask(replace=replace)
        )

    async def confirm(self, callback: CallbackQuery, callback_data: Run) -> None:
        """«Перенести» или «Отмена» под отчётом."""
        # Отвечаем Telegram сразу: у нажатия свой таймаут, и он куда короче
        # цепочки. Клавиатура снимается тем же движением — второй раз нажать
        # уже нельзя, а повторный запуск всё равно упёрся бы в замок.
        await callback.answer()
        if not isinstance(callback.message, Message):
            return

        if not callback_data.go:
            await callback.message.edit_text(texts.CANCELLED, reply_markup=None)
            return

        await callback.message.edit_text("Переношу…", reply_markup=None)
        task = asyncio.create_task(
            self.work(callback.message, replace=callback_data.replace)
        )
        RUNNING.add(task)
        task.add_done_callback(RUNNING.discard)

    async def work(self, note: Message, *, replace: bool) -> None:
        """Прогнать цепочку, дописывая в сообщение каждый пройденный шаг."""
        done: list[str] = []
        try:
            async for step in self.engine.finish(replace=replace):
                if step.failed:
                    await note.edit_text(
                        texts.progress(done) + "\n" + texts.failure(step.name, step.output)
                    )
                    return
                done.append(step.name)
                await note.edit_text(texts.progress(done))
        except Busy:
            await note.edit_text(texts.BUSY)
        except Exception:  # pylint: disable=broad-exception-caught
            # Задача идёт сама по себе: некому поймать её исключение и некому
            # о нём рассказать, кроме нас. Молча пропавший импорт — худшее,
            # что здесь может случиться.
            log.exception("цепочка импорта сорвалась")
            await note.edit_text(texts.progress(done) + "\n❌ Сорвалось, смотрите логи.")

    @staticmethod
    def _ask(*, replace: bool) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Перенести",
                        callback_data=Run(go=True, replace=replace).pack(),
                    ),
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=Run(go=False, replace=replace).pack(),
                    ),
                ]
            ]
        )
