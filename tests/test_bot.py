"""Тесты бота в Telegram.

Проверяется разговор: кого бот слушает, что делает с присланным файлом, о чём
спрашивает и где останавливается. Настоящий Telegram для этого не нужен и не
годится — апдейты собираются руками и скармливаются диспетчеру.

Подделан ровно транспорт: сессия, которая никуда не ходит и запоминает вызовы.
Роутеры, фильтры, middleware и разбор callback_data при этом настоящие — тот
же приём, что и FakeConsole в tests/test_add.py, и по той же причине: подделав
их, мы проверяли бы подделку.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    File,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Update,
    User,
)

from finance.bot import access, keys, texts
from finance.bot.app import BotApp
from finance.bot.engine import Engine
from finance.bot.settings import MAX_BYTES, Settings
from tests.fixtures import AMERIA_ACCOUNT_DIR, AMERIA_CARD_DIR

CARD = AMERIA_CARD_DIR / "card0001_statement.csv"
ACCOUNT = AMERIA_ACCOUNT_DIR / "usd_statement.csv"

MINE = 111111
STRANGER = 222222


class Recorder(BaseSession):
    """Сессия, которая никуда не ходит: помнит вызовы и отвечает заготовкой."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.files: dict[str, bytes] = {}
        self.message_id = 0
        #: Ответы приходится привязывать к боту: без этого у Message нет
        #: способа что-либо ответить, и хендлеры падают на пустом месте.
        self.bot: Bot | None = None

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        """Запомнить вызов и ответить тем, что вернул бы Telegram."""
        self.calls.append(method)
        return self._answer(method)

    async def stream_content(self, *args: Any, **kwargs: Any):
        """Содержимое «скачанного» файла — по кускам, как настоящая сессия."""
        url = args[0] if args else kwargs.get("url", "")
        yield self.files.get(url, b"")

    async def close(self) -> None:
        """Закрывать нечего: соединений тут нет."""

    def _answer(self, method: TelegramMethod) -> Any:
        name = type(method).__name__
        if name in ("SendMessage", "EditMessageText"):
            return self._message(method)
        if name == "GetFile":
            return File(
                file_id=method.file_id, file_unique_id=method.file_id, file_path=method.file_id
            )
        return True

    def _message(self, method: TelegramMethod) -> Message:
        self.message_id += 1
        return Message(
            message_id=self.message_id,
            date=dt.datetime.now(dt.timezone.utc),
            chat=Chat(id=MINE, type="private"),
            text=getattr(method, "text", ""),
            reply_markup=getattr(method, "reply_markup", None),
        ).as_(self.bot)

    # Путь к файлу подделывается заодно: настоящий Telegram отдаёт file_path
    # в GetFile, а по нему сессия качает содержимое.
    def upload(self, file_id: str, data: bytes) -> None:
        """Положить содержимое, которое «скачается» по этому file_id."""
        self.files[f"https://api.telegram.org/file/bot42:TEST/{file_id}"] = data

    def said(self) -> list[str]:
        """Всё, что бот сказал, по порядку."""
        return [
            call.text
            for call in self.calls
            if type(call).__name__ in ("SendMessage", "EditMessageText")
        ]

    def keyboards(self) -> list[InlineKeyboardMarkup]:
        """Все клавиатуры, что бот показал."""
        return [
            call.reply_markup
            for call in self.calls
            if getattr(call, "reply_markup", None) is not None
        ]

    def called(self, name: str) -> bool:
        """Звался ли такой метод Telegram."""
        return any(type(call).__name__ == name for call in self.calls)


@pytest.fixture
def chat(finance_env):
    """Бот со своим inbox, готовый принимать апдейты."""
    session = Recorder()
    settings = Settings(
        token="42:TEST",
        allowed=frozenset({MINE}),
        path="hook",
        secret="secret-secret-1",
        public_url="https://x.example",
    )
    bot = Bot(settings.token, session=session)
    session.bot = bot
    app = BotApp(settings, Engine(finance_env.parent / "run"))
    app.bot = bot
    return Chatting(app, bot, session, finance_env)


class Chatting:
    """Что можно сделать в чате и что после этого лежит в inbox."""

    def __init__(self, app: BotApp, bot: Bot, session: Recorder, inbox) -> None:
        self.app = app
        self.bot = bot
        self.session = session
        self.inbox = inbox
        self.update_id = 0

    async def say(self, text: str, chat_id: int = MINE) -> None:
        """Написать боту."""
        await self._feed(Update(update_id=self._next(), message=self._message(text, chat_id)))

    async def send(self, name: str, data: bytes, size: int | None = None) -> None:
        """Прислать файл."""
        file_id = f"file-{self._next()}"
        self.session.upload(file_id, data)
        message = self._message("", MINE)
        message = message.model_copy(
            update={
                "document": Document(
                    file_id=file_id,
                    file_unique_id=file_id,
                    file_name=name,
                    file_size=size if size is not None else len(data),
                )
            }
        )
        await self._feed(Update(update_id=self._next(), message=message))

    async def photo(self) -> None:
        """Прислать картинку вместо файла."""
        message = self._message("", MINE).model_copy(
            update={
                "photo": [
                    PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)
                ]
            }
        )
        await self._feed(Update(update_id=self._next(), message=message))

    async def tap(self, data: str, chat_id: int = MINE) -> None:
        """Нажать кнопку."""
        await self._feed(
            Update(
                update_id=self._next(),
                callback_query=CallbackQuery(
                    id=str(self._next()),
                    from_user=User(id=chat_id, is_bot=False, first_name="Кто"),
                    chat_instance="1",
                    data=data,
                    message=self._message("отчёт", chat_id),
                ).as_(self.bot),
            )
        )

    def allow(self, allowed: frozenset[int]) -> None:
        """Переписать список допущенных и пересобрать диспетчер под него."""
        self.app.settings = replace(self.app.settings, allowed=allowed)
        self.app.dispatcher = self.app.build_dispatcher()

    def names(self) -> list[str]:
        """Что лежит в inbox — по именам: имя тут и есть предмет проверки."""
        return sorted(path.name for path in self.inbox.iterdir())

    def said(self) -> str:
        return "\n".join(self.session.said())

    def buttons(self) -> list[str]:
        """Данные всех кнопок, что бот показал."""
        return [
            button.callback_data
            for markup in self.session.keyboards()
            for row in markup.inline_keyboard
            for button in row
        ]

    async def _feed(self, update: Update) -> None:
        await self.app.dispatcher.feed_update(self.bot, update)

    def _message(self, text: str, chat_id: int) -> Message:
        return Message(
            message_id=self._next(),
            date=dt.datetime.now(dt.timezone.utc),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="Кто"),
            text=text,
        ).as_(self.bot)

    def _next(self) -> int:
        self.update_id += 1
        return self.update_id


# ──────────────────────────── кого бот слушает ────────────────────────────


async def test_stranger_is_turned_away(chat):
    await chat.say("/start", chat_id=STRANGER)
    assert access.STRANGER in chat.said()


async def test_stranger_cannot_reach_the_inbox(chat):
    """Отказ должен стоять раньше хендлеров, а не после них."""
    await chat.send("statement_march.csv", ACCOUNT.read_bytes())
    ours = chat.names()

    await chat.say("/inbox", chat_id=STRANGER)

    assert chat.names() == ours, "чужой не должен ничего узнать про inbox"


async def test_empty_allowlist_tells_you_your_chat_id(chat):
    """Так и узнают свой chat id: другого способа нет, а бот с пустым списком
    ровно за этим и запускается."""
    chat.allow(frozenset())

    await chat.say("/start")

    assert str(MINE) in chat.said()


async def test_empty_allowlist_still_lets_nobody_in(chat):
    """Назвать id — не то же самое, что пустить."""
    chat.allow(frozenset())

    await chat.send("statement_march.csv", ACCOUNT.read_bytes())

    assert chat.names() == []


# ──────────────────────────── приём выписок ────────────────────────────


async def test_settled_statement_lands_as_is(chat):
    await chat.send("statement_march.csv", ACCOUNT.read_bytes())

    assert chat.names() == ["statement_march.csv"]
    assert "Assets:Ameria:Usd" in chat.said()


async def test_ambiguous_statement_is_kept_and_asked_about(chat):
    """Отвергнуть файл значило бы заставить присылать его заново с телефона."""
    await chat.send("export_777.csv", CARD.read_bytes())

    assert chat.names() == ["export_777.csv"], "файл всё равно сохранён"
    assert chat.buttons(), "и про него спрошено кнопками"


async def test_choosing_an_account_renames_the_file(chat):
    await chat.send("export_777.csv", CARD.read_bytes())
    await chat.tap(
        f"acc:{keys.of('export_777.csv')}:{keys.of('Assets:Ameria:Card0002')}"
    )

    assert chat.names() == ["card0002_export_777.csv"]


async def test_skipping_leaves_the_file_alone(chat):
    await chat.send("export_777.csv", CARD.read_bytes())
    await chat.tap(f"acc:{keys.of('export_777.csv')}:{keys.NOTHING}")

    assert chat.names() == ["export_777.csv"]


async def test_button_for_a_vanished_file_says_so(chat):
    """Файл могли унести через fava или разобрать, пока сообщение висело."""
    await chat.tap(f"acc:{keys.of('нет-такого.csv')}:{keys.of('Assets:Ameria:Card0001')}")

    assert "больше нет" in chat.said()


async def test_upload_strips_directories_from_the_name(chat):
    """Имя приходит из чата, и подниматься по нему из inbox нельзя."""
    await chat.send("../../statement_march.csv", ACCOUNT.read_bytes())

    assert chat.names() == [".. .. statement_march.csv"]


async def test_oversized_file_is_refused_before_downloading(chat):
    await chat.send("huge.csv", b"x", size=MAX_BYTES + 1)

    assert chat.names() == []
    assert not chat.session.called("GetFile"), "качать его незачем"


async def test_wrong_suffix_is_refused(chat):
    await chat.send("virus.exe", b"MZ")

    assert chat.names() == []
    assert not chat.session.called("GetFile")


async def test_photo_is_asked_to_be_a_file(chat):
    """Telegram пережимает картинки, разбирать нужно исходник."""
    await chat.photo()

    assert texts.NOT_A_FILE in chat.said()


# ──────────────────────────── что лежит в inbox ────────────────────────────


async def test_inbox_lists_every_state(chat):
    """Опознанный, спорный и вовсе лишний файл — все три должны быть видны."""
    await chat.send("statement_march.csv", ACCOUNT.read_bytes())
    await chat.send("export_777.csv", CARD.read_bytes())
    (chat.inbox / "notes.txt").write_text("лишний файл\n", encoding="utf-8")

    await chat.say("/inbox")
    said = chat.said()

    assert "Assets:Ameria:Usd" in said
    assert "export_777.csv" in said
    assert "notes.txt" in said


async def test_inbox_skips_service_files(chat):
    (chat.inbox / ".gitkeep").write_text("", encoding="utf-8")

    await chat.say("/inbox")

    assert texts.EMPTY_INBOX in chat.said()


async def test_empty_inbox_says_so(chat):
    await chat.say("/inbox")
    assert texts.EMPTY_INBOX in chat.said()


async def test_start_explains_itself(chat):
    await chat.say("/start")
    assert "/import" in chat.said()
