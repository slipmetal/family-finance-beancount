"""Тот же движок импорта, только вызываемый из корутин.

Шаги finance/pipeline.py — подпроцессы: они держат поток секундами, а курсы
валют ещё и ходят в сеть. В событийном цикле такое звать нельзя, иначе бот
перестаёт отвечать на вебхук, Telegram считает доставку неудачной и присылает
тот же апдейт заново. Поэтому каждый вызов уезжает в поток.

Ничего своего про раскладку и про шаги здесь нет — только замок и потоки.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory

from finance import config
from finance.bot import keys
from finance.categorize import Rules
from finance.inbox import Account, Inbox, Row, describe, plain_name
from finance.pipeline import Extraction, Pipeline, Step

log = logging.getLogger(__name__)

#: Сколько последних update_id помним. Telegram повторяет доставку, если 200
#: не дошёл, — а повторно принятый документ лёг бы в inbox вторым файлом.
REMEMBERED = 200


class Busy(Exception):
    """Импорт уже идёт.

    Второй в очередь не встаёт: он дрался бы за out.beancount и за файлы
    леджера с первым. Ждать в чате молча минуту хуже, чем услышать отказ.
    """


class Engine:
    """Раскладка и шаги импорта, пригодные для разговора."""

    def __init__(self, run: Path | None = None) -> None:
        # Как и пути в config, берётся в момент создания: тесты подменяют его
        # там же, где и остальные, и на настоящий диск ничего не попадает.
        self.run = run if run is not None else config.RUN
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        """Идёт ли разбор прямо сейчас."""
        return self._lock.locked()

    # ─────────────────────────── раскладка ───────────────────────────
    #
    # Замок здесь не берётся намеренно: загрузка быстрая и трогает только
    # inbox. Плата — файл, положенный во время разбора, попадёт в следующий,
    # а не в идущий. Это честнее, чем заставлять человека ждать с телефоном
    # в руках, пока досчитаются курсы валют.

    async def rows(self) -> list[Row]:
        """Что лежит в inbox и кому достанется."""
        return await asyncio.to_thread(self._rows)

    async def accounts(self) -> list[Account]:
        """Все счета — из них собираются кнопки выбора."""
        return await asyncio.to_thread(lambda: self._inbox().accounts)

    async def take(self, name: str, data: bytes, account_key: str = "") -> str:
        """Принять присланный файл. Возвращает фразу о том, чем всё кончилось.

        Повторяет решение страницы «Выписки»: счёт не выбран и по содержимому
        не определился — файл всё равно ложится. Отвергнуть его значило бы
        заставить человека присылать выписку с телефона заново, а это худшая
        мена, чем строчка «выберите счёт» под сообщением.
        """
        return await asyncio.to_thread(self._take, name, data, account_key)

    async def assign(self, file_key: str, account_key: str) -> str:
        """Назвать счёт файлу, который уже лежит в inbox."""
        return await asyncio.to_thread(self._assign, file_key, account_key)

    # ──────────────────────────── импорт ────────────────────────────

    async def extract(self) -> Extraction:
        """Разобрать всё, что лежит в inbox. Занято — Busy."""
        if self.busy:
            raise Busy
        async with self._lock:
            return await asyncio.to_thread(self._pipeline().extract)

    async def finish(self, *, replace: bool = False) -> AsyncIterator[Step]:
        """Перенос, курсы, архив, проверка — по одному шагу, до первой неудачи.

        Порядок и остановка живут в Pipeline.finish; здесь только перекладка
        его в потоки. На время работы кладётся замок-файл: по нему
        deploy/entrypoint.py понимает, что останавливаться пока нельзя.
        """
        if self.busy:
            raise Busy
        async with self._lock:
            with self._working():
                steps = self._pipeline().finish(replace=replace)
                # Двухаргументный next: StopIteration, вылетевшая из потока,
                # превратилась бы в невнятный RuntimeError.
                while (step := await asyncio.to_thread(next, steps, None)) is not None:
                    yield step

    # ──────────────────────────── повторы ────────────────────────────

    def seen(self, update_id: int) -> bool:
        """Видели ли мы этот апдейт раньше.

        Telegram повторяет доставку, если 200 не дошёл, — а машину могли
        усыпить ровно между работой и ответом. Без этой памяти повторно
        принятый документ лёг бы в inbox вторым файлом под именем `…-2`.
        """
        kept = self.run / "seen-updates.json"
        try:
            known = json.loads(kept.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known = []
        if update_id in known:
            return True
        known = [*known[-REMEMBERED:], update_id]
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text(json.dumps(known), encoding="utf-8")
        return False

    # ─────────────────────────── внутреннее ───────────────────────────

    @staticmethod
    def _inbox() -> Inbox:
        """Раскладка на один вызов.

        Пересобирается каждый раз, как и на странице «Выписки»: accounts.yaml
        и rules.yaml правятся при живом боте — руками в fava и приездом из
        git, — а файлы в inbox появляются и вовсе мимо него.
        """
        return Inbox.build()

    def _pipeline(self) -> Pipeline:
        rules = Rules.load(config.RULES)
        inbox = Inbox.build(rules)
        return Pipeline(inbox=inbox.directory, uncategorized=rules.default_account)

    def _rows(self) -> list[Row]:
        inbox = self._inbox()
        if not inbox.directory.exists():
            return []
        return [
            describe(inbox, path)
            for path in sorted(inbox.directory.iterdir())
            # `.gitkeep` и прочие точечные файлы — служебные, не выписки.
            if path.is_file() and not path.name.startswith(".")
        ]

    def _take(self, name: str, data: bytes, account_key: str) -> str:
        inbox = self._inbox()
        account = self._account(inbox, account_key)
        with TemporaryDirectory(prefix="finance-bot-") as tmp:
            source = Path(tmp) / plain_name(name)
            source.write_bytes(data)
            if account is None and not inbox.verdict(source).settled:
                kept = inbox.keep(source)
                return f"{kept.name}: счёт не определён."
            target, owner = inbox.place(source, account)
            return f"{target.name} → {owner.name}"

    def _assign(self, file_key: str, account_key: str) -> str:
        inbox = self._inbox()
        account = self._account(inbox, account_key)
        if account is None:
            return "Такого счёта в accounts.yaml нет."
        source = self._file(inbox, file_key)
        if source is None:
            return "Этого файла в inbox больше нет."
        target, owner = inbox.place(source, account)
        return f"{target.name} → {owner.name}"

    @staticmethod
    def _account(inbox: Inbox, key: str) -> Account | None:
        """Счёт по ключу кнопки. Пусто — «разобраться по содержимому файла»."""
        if not key:
            return None
        return keys.find(inbox.accounts, lambda account: account.name, key)

    @staticmethod
    def _file(inbox: Inbox, key: str) -> Path | None:
        if not inbox.directory.exists():
            return None
        return keys.find(
            (path for path in inbox.directory.iterdir() if path.is_file()),
            lambda path: path.name,
            key,
        )

    def _working(self):
        """Замок-файл на время работы: остановка контейнера его дождётся."""
        return _Working(self.run / "import.lock")


class _Working:
    """Отметка «идёт импорт», видимая из другого процесса."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> _Working:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.path.unlink(missing_ok=True)
