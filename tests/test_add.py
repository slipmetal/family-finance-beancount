"""Тесты сценария `import.py add`.

Проверяется разговор, а не разбор: у кого команда спрашивает, у кого не
спрашивает и где останавливается. Сама цепочка шагов подставная — настоящая
пишет в леджер, а он в приватном репозитории, и в тестах ему делать нечего.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import amount, data

from finance.add import AddCommand
from finance.pipeline import Extraction, Step
from tests.conftest import copy
from tests.fixtures import AMERIA_ACCOUNT_DIR, AMERIA_CARD_DIR

CARD = AMERIA_CARD_DIR / "card0001_statement.csv"
ACCOUNT = AMERIA_ACCOUNT_DIR / "usd_statement.csv"


class FakeConsole:
    """Заранее заготовленные ответы вместо человека за клавиатурой."""

    def __init__(self, confirms: list[bool] | None = None, choices: list[int | None] | None = None):
        self.confirms = list(confirms or [])
        self.choices = list(choices or [])
        self.questions: list[str] = []
        self.lines: list[str] = []

    def say(self, message: str = "") -> None:
        self.lines.append(message)

    step = warn = fail = ok = say

    def confirm(self, question: str, *, default: bool = True) -> bool:
        self.questions.append(question)
        return self.confirms.pop(0)

    def choose(self, question: str, options: list[str]) -> int | None:
        self.questions.append(question)
        return self.choices.pop(0)

    @property
    def said(self) -> str:
        return "\n".join(self.lines)


class FakePipeline:
    """Считает вызовы вместо того, чтобы что-то делать с леджером."""

    def __init__(self, extraction: Extraction, failing: str = "") -> None:
        self.extraction = extraction
        self.failing = failing
        self.calls: list[str] = []
        self.out = Path("out.beancount")
        self.main = Path("ledger/main.beancount")

    def extract(self) -> Extraction:
        self.calls.append("extract")
        return self.extraction

    def merge(self, *, replace: bool = False) -> Step:
        self.calls.append(f"merge(replace={replace})")
        return self._step("Перенос в леджер")

    def rates(self) -> Step:
        self.calls.append("rates")
        return self._step("Курсы валют")

    def archive(self) -> Step:
        self.calls.append("archive")
        return self._step("Архив выписок")

    def check(self) -> Step:
        self.calls.append("check")
        return self._step("Проверка леджера")

    def _step(self, name: str) -> Step:
        ok = self.calls[-1].split("(")[0] != self.failing
        return Step(name, ok, "" if ok else "подробности ошибки")


def transaction(payee: str) -> data.Transaction:
    """Проводка без категории — такую команда и показывает перед переносом."""
    return data.Transaction(
        meta=data.new_metadata("<test>", 0),
        date=dt.date(2026, 8, 3),
        flag="!",
        payee=payee,
        narration="",
        tags=set(),
        links=set(),
        postings=[
            data.Posting(
                "Assets:Ameria:Card0001",
                amount.Amount(Decimal("-15000.00"), "AMD"),
                None, None, None, None,
            ),
            data.Posting("Expenses:Uncategorized", None, None, None, None, None),
        ],
    )


def extraction(transactions: int = 5, uncategorized: int = 0) -> Extraction:
    """Разбор с нужным числом проводок."""
    return Extraction(
        "Разбор выписок",
        True,
        transactions=transactions,
        uncategorized=[transaction(f"Контрагент {number}") for number in range(uncategorized)],
    )


@pytest.fixture
def drop(tmp_path) -> Path:
    """Папка, куда «скачаны» выписки."""
    path = tmp_path / "drop"
    path.mkdir()
    return path


def make(console: FakeConsole, inbox, pipeline: FakePipeline) -> AddCommand:
    return AddCommand(console, inbox, pipeline)


def run(command: AddCommand, sources: list[Path], **kwargs) -> int:
    options = {"move": True, "assume_yes": True, "replace": False, **kwargs}
    return command.run(sources, **options)


# ─────────────────────────── о чём спрашивают ───────────────────────────


def test_settled_file_goes_through_without_questions(inbox, drop):
    """Счёт написан в самой выписке — человека дёргать не за чем."""
    source = copy(ACCOUNT, drop, "statement_march.csv")
    console = FakeConsole()

    assert run(make(console, inbox, FakePipeline(extraction())), [source]) == 0
    assert console.questions == []
    assert (inbox.directory / "statement_march.csv").exists()


def test_ambiguous_file_is_asked_about_and_renamed(inbox, drop):
    """Две карты в AMD: по файлу не понять, поэтому вопрос — и переименование."""
    source = copy(CARD, drop, "export_777.csv")
    console = FakeConsole(choices=[1])

    assert run(make(console, inbox, FakePipeline(extraction())), [source]) == 0
    assert len(console.questions) == 1
    assert (inbox.directory / "card0002_export_777.csv").exists()
    assert not source.exists(), "исходник переехал, а не скопировался"


def test_skipped_file_stays_where_it_was(inbox, drop):
    """Отказ отвечать — не повод угадывать: файл остаётся лежать как лежал,
    а импорт не начинается, раз раскладывать нечего."""
    source = copy(CARD, drop, "export_777.csv")
    console = FakeConsole(choices=[None])
    pipeline = FakePipeline(extraction())

    assert run(make(console, inbox, pipeline), [source]) == 1
    assert source.exists()
    assert not inbox.directory.exists()
    assert pipeline.calls == []


def test_files_already_in_inbox_are_left_in_place(inbox, drop):
    """`add` без аргументов разбирает inbox — переименовывать там нечего."""
    inbox.directory.mkdir(parents=True)
    source = copy(ACCOUNT, inbox.directory, "statement_march.csv")
    console = FakeConsole()

    run(make(console, inbox, FakePipeline(extraction())), [inbox.directory])

    assert source.exists()
    assert list(inbox.directory.iterdir()) == [source]


def test_unknown_file_is_reported_not_guessed(inbox, drop):
    """Не выписка — так и говорим, а не подсовываем первому попавшемуся счёту."""
    source = drop / "notes.csv"
    source.write_text("не то и не в том формате\n", encoding="utf-8")
    console = FakeConsole()

    run(make(console, inbox, FakePipeline(extraction())), [source])

    assert console.questions == []
    assert "не опознан" in console.said
    assert source.exists()


# ────────────────────────── где останавливаются ──────────────────────────


def test_no_at_the_checkpoint_stops_before_the_ledger(inbox, drop):
    """Просмотр разобранного — тот самый шаг, который нельзя проскочить."""
    source = copy(ACCOUNT, drop, "statement_march.csv")
    console = FakeConsole(confirms=[False])
    pipeline = FakePipeline(extraction(uncategorized=2))

    assert run(make(console, inbox, pipeline), [source], assume_yes=False) == 0
    assert pipeline.calls == ["extract"]
    assert "Перенести в леджер?" in console.questions


def test_yes_runs_the_rest_in_order(inbox, drop):
    source = copy(ACCOUNT, drop, "statement_march.csv")
    pipeline = FakePipeline(extraction())

    assert run(make(FakeConsole(), inbox, pipeline), [source], replace=True) == 0
    assert pipeline.calls == ["extract", "merge(replace=True)", "rates", "archive", "check"]


def test_failed_step_stops_the_chain(inbox, drop):
    """Курсы не обновились — архивировать и проверять уже нечего смотреть."""
    source = copy(ACCOUNT, drop, "statement_march.csv")
    pipeline = FakePipeline(extraction(), failing="rates")
    console = FakeConsole()

    assert run(make(console, inbox, pipeline), [source]) == 1
    assert pipeline.calls == ["extract", "merge(replace=False)", "rates"]
    assert "подробности ошибки" in console.said


def test_nothing_new_stops_before_the_ledger(inbox, drop):
    """Все проводки уже перенесены: переносить нечего, архивировать рано."""
    source = copy(ACCOUNT, drop, "statement_march.csv")
    pipeline = FakePipeline(extraction(transactions=0))

    assert run(make(FakeConsole(), inbox, pipeline), [source]) == 0
    assert pipeline.calls == ["extract"]


def test_no_sources_is_an_error_not_a_silent_run(inbox, tmp_path):
    console = FakeConsole()
    pipeline = FakePipeline(extraction())

    assert run(make(console, inbox, pipeline), [tmp_path / "нет-такой-папки"]) == 1
    assert pipeline.calls == []
