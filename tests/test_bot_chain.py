"""Тесты команды /import — той части бота, что доходит до леджера.

Сама цепочка шагов подставная, как и в tests/test_add.py: настоящая пишет
в леджер, а он в приватном репозитории, и в тестах ему делать нечего.
Проверяется здесь ровно разговор вокруг неё — о чём бот спрашивает, что
показывает и где останавливается.
"""

from __future__ import annotations

from finance.bot import texts
from finance.bot.chain import Chain, Run
from finance.bot.engine import Busy
from finance.pipeline import Extraction, Step


class FakeEngine:
    """Считает вызовы вместо того, чтобы что-то делать с леджером."""

    def __init__(self, extraction: Extraction, failing: str = "") -> None:
        self.extraction = extraction
        self.failing = failing
        self.calls: list[str] = []
        self.busy = False

    async def extract(self) -> Extraction:
        self.calls.append("extract")
        if self.busy:
            raise Busy
        return self.extraction

    async def finish(self, *, replace: bool = False):
        """Те же четыре шага в том же порядке, что и у настоящего Pipeline."""
        self.calls.append(f"finish(replace={replace})")
        for name in ("Перенос в леджер", "Курсы валют", "Архив выписок", "Проверка леджера"):
            self.calls.append(name)
            ok = name != self.failing
            yield Step(name, ok, "" if ok else "подробности ошибки")
            if not ok:
                return


class Note:
    """Сообщение, которое бот правит по ходу дела."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def edit_text(self, text: str, reply_markup: object = None) -> None:
        self.texts.append(text)
        self.markups.append(reply_markup)

    @property
    def last(self) -> str:
        return self.texts[-1] if self.texts else ""


def extraction(transactions: int = 3, failed: bool = False) -> Extraction:
    return Extraction(
        "Разбор выписок",
        not failed,
        "" if not failed else "beangulp упал",
        transactions=transactions,
    )


async def run_chain(engine: FakeEngine, *, replace: bool = False) -> Note:
    """Прогнать цепочку так, как её гоняет кнопка «Перенести»."""
    note = Note()
    await Chain(engine).work(note, replace=replace)
    return note


# ──────────────────────────── порядок шагов ────────────────────────────


async def test_steps_run_in_order():
    engine = FakeEngine(extraction())

    await run_chain(engine)

    assert engine.calls == [
        "finish(replace=False)",
        "Перенос в леджер",
        "Курсы валют",
        "Архив выписок",
        "Проверка леджера",
    ]


async def test_failed_merge_stops_before_the_archive():
    """Главное, ради чего порядок вообще соблюдается: архив уносит выписки
    из inbox, и после несостоявшегося переноса они уехали бы в никуда."""
    engine = FakeEngine(extraction(), failing="Перенос в леджер")

    note = await run_chain(engine)

    assert "Архив выписок" not in engine.calls
    assert "не получилось" in note.last


async def test_failure_shows_what_was_said():
    engine = FakeEngine(extraction(), failing="Курсы валют")

    note = await run_chain(engine)

    assert "подробности ошибки" in note.last
    assert "Перенос в леджер" in note.last, "пройденное тоже видно"


async def test_replace_reaches_the_pipeline():
    """Ключ нужен после правки rules.yaml, иначе проводки лягут вторым разом."""
    engine = FakeEngine(extraction())

    await run_chain(engine, replace=True)

    assert "finish(replace=True)" in engine.calls


async def test_a_broken_step_does_not_vanish_silently():
    """Задача идёт сама по себе — некому поймать её исключение, кроме неё."""

    class Exploding(FakeEngine):
        """Движок, который ломается на первом же шаге."""

        async def finish(self, *, replace: bool = False):
            # Пустой цикл делает метод асинхронным генератором — без него
            # `raise` сработал бы при вызове, а не при переборе, и проверка
            # получилась бы не про то.
            for _ in ():
                yield
            raise RuntimeError("что-то сломалось")

    note = await run_chain(Exploding(extraction()))

    assert "Сорвалось" in note.last


# ──────────────────────────── отчёт о разборе ────────────────────────────


def test_report_counts_what_was_parsed():
    said = texts.report(Extraction("Разбор", True, "", transactions=7, duplicates=2))

    assert "7" in said
    assert "2" in said


def test_empty_extraction_is_not_worth_a_button():
    assert Extraction("Разбор", True, "", transactions=0).empty


def test_failure_output_is_cut_to_fit_a_message():
    """В сообщение Telegram влезает 4096 символов, а вывод bean-check длиннее."""
    said = texts.failure("Проверка леджера", "строка\n" * 5000)

    assert len(said) < 4096


# ──────────────────────────── кнопка ────────────────────────────


def test_button_fits_the_callback_limit():
    """У callback_data 64 байта на всё."""
    assert len(Run(go=True, replace=True).pack().encode()) <= 64
