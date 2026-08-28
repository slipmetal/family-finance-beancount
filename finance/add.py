"""Команда `import.py add`: положить выписки и прогнать весь импорт разом.

    python import.py add ~/Downloads/*.csv
    python import.py add                      # разобрать то, что уже в inbox

Заменяет собой шесть команд из README, а главное — снимает необходимость
называть файлы руками. Счёт почти всегда написан внутри выписки; там, где не
написан (карты Ameriabank в одной валюте), команда спрашивает и переименовывает
файл сама.

Спрашивает она только про то, что неоднозначно, и по делу дважды: какой счёт,
если по файлу этого не понять, и переносить ли разобранное в леджер. Второй
вопрос — тот самый просмотр out.beancount, на котором в README настаивают:
перед ним печатается, сколько проводок разобрано, сколько сочтено дубликатами
и что осталось без категории.

Вся работа лежит в двух немых модулях — finance/inbox.py (кто заберёт файл и
как его назвать) и finance/pipeline.py (шаги импорта). Здесь только разговор
с человеком. Разделение не ради красоты: тот же движок нужен боту в Telegram
и форме в fava, а вот `input()` им не нужен ни в каком виде.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import click
from beancount.core import data

from finance.categorize import Rules
from finance.config import RULES
from finance.inbox import Account, Inbox, InboxError, Verdict
from finance.pipeline import Extraction, Pipeline, Step

#: Сколько проводок без категории печатать списком. Дальше он перестаёт
#: помещаться в экран, а решение принимается и по первым.
SHOWN = 15


class Console:
    """Разговор с человеком в терминале.

    Отдельным классом, чтобы движок остался немым: у бота на этом месте будут
    кнопки в чате, и заменить нужно будет только это.
    """

    def say(self, message: str = "") -> None:
        click.echo(message)

    def step(self, message: str) -> None:
        click.secho(message, bold=True)

    def ok(self, message: str) -> None:
        click.secho(message, fg="green")

    def warn(self, message: str) -> None:
        click.secho(message, fg="yellow")

    def fail(self, message: str) -> None:
        click.secho(message, fg="red", err=True)

    def confirm(self, question: str, *, default: bool = True) -> bool:
        return click.confirm(question, default=default)

    def choose(self, question: str, options: list[str]) -> int | None:
        """Выбор из списка. None — человек решил пропустить."""
        self.say(question)
        for number, option in enumerate(options, start=1):
            self.say(f"  {number}. {option}")
        self.say("  0. пропустить файл")
        choice = click.prompt(
            "  номер", type=click.IntRange(0, len(options)), default=0, show_default=False
        )
        return None if choice == 0 else choice - 1


class AddCommand:
    """Сценарий `add`: разложить файлы, разобрать, показать, перенести."""

    def __init__(self, console: Console, inbox: Inbox, pipeline: Pipeline) -> None:
        self.console = console
        self.inbox = inbox
        self.pipeline = pipeline

    # ─────────────────────────────── сценарий ───────────────────────────────

    def run(self, sources: list[Path], *, move: bool, assume_yes: bool, replace: bool) -> int:
        """Сценарий целиком. Возвращает код возврата процесса."""
        files = self._collect(sources)
        if not files:
            self.console.warn("Нечего импортировать: не передан ни один файл.")
            return 1

        placed, skipped = self._place_all(files, move=move)
        if not placed:
            self.console.fail(
                "Ни один файл не разложен по счетам — импортировать нечего."
            )
            return 1

        extraction = self.pipeline.extract()
        self._report(extraction)
        if extraction.failed:
            self.console.fail(extraction.output)
            return 1
        if extraction.empty:
            self.console.warn(
                "Новых проводок нет — переносить нечего. "
                "Выписки остались в inbox, архивировать их нечем."
            )
            return 0

        if not assume_yes and not self.console.confirm("Перенести в леджер?"):
            self.console.say(
                f"Остановились. Разобранное лежит в {self.pipeline.out}; "
                "поправьте правила в rules.yaml и запустите снова."
            )
            return 0

        # Шаги передаются невыполненными: первый же провал должен остановить
        # остальные. Особенно важно для архива — он уносит выписки из inbox,
        # и делать это после несостоявшегося переноса нельзя.
        return self._finish(
            [
                lambda: self.pipeline.merge(replace=replace),
                self.pipeline.rates,
                self.pipeline.archive,
                self.pipeline.check,
            ],
            skipped,
        )

    # ──────────────────────────── раскладка файлов ────────────────────────────

    def _collect(self, sources: list[Path]) -> list[Path]:
        """Развернуть папки в файлы. Порядок — по имени, чтобы вывод не прыгал."""
        files: list[Path] = []
        for source in sources:
            if source.is_dir():
                files += sorted(child for child in source.iterdir() if child.is_file())
            elif source.is_file():
                files.append(source)
            else:
                self.console.warn(f"{source}: нет такого файла, пропущено")
        return files

    def _place_all(self, files: list[Path], *, move: bool) -> tuple[list[Path], list[Path]]:
        placed: list[Path] = []
        skipped: list[Path] = []
        for path in files:
            try:
                target = self._place_one(path, move=move)
            except InboxError as error:
                self.console.fail(str(error))
                target = None
            (placed if target else skipped).append(target or path)
        return [path for path in placed if path], skipped

    def _place_one(self, path: Path, *, move: bool) -> Path | None:
        verdict = self.inbox.verdict(path)
        settled_here = verdict.settled and self._in_inbox(path)

        if settled_here:
            # Файл уже лежит там, где надо, и уже опознан: трогать нечего.
            self.console.say(f"{path.name} → {verdict.owners[0].name} (уже в inbox)")
            return path

        account = self._resolve(verdict)
        if account is None and not verdict.settled:
            return None

        target, owner = self.inbox.place(path, account, move=move)
        moved = "перемещён" if move else "скопирован"
        renamed = "" if target.name == path.name else f", переименован в {target.name}"
        self.console.say(f"{path.name} → {owner.name} ({moved}{renamed})")
        return target

    def _resolve(self, verdict: Verdict) -> Account | None:
        """Понять, какому счёту принадлежит файл. Спрашиваем только если надо."""
        if verdict.settled:
            return None  # имя менять не нужно, счёт подтвердится при раскладке
        if verdict.disputed:
            owners = ", ".join(owner.name for owner in verdict.owners)
            self.console.fail(
                f"{verdict.path.name}: файл забирают сразу несколько счетов ({owners}). "
                "На таком падает и обычный импорт — уберите из имени лишнюю метку."
            )
            return None
        if verdict.unknown:
            self.console.warn(
                f"{verdict.path.name}: не опознан ни одним импортёром. "
                "Либо это не выписка, либо банк для неё ещё не написан."
            )
            return None

        options = [account.name for account in verdict.candidates]
        if len(options) == 1:
            question = f"{verdict.path.name}: это выписка по счёту {options[0]}?"
            return verdict.candidates[0] if self.console.confirm(question) else None

        chosen = self.console.choose(
            f"{verdict.path.name}: по содержимому счёт не определить. Чей это файл?",
            options,
        )
        return None if chosen is None else verdict.candidates[chosen]

    def _in_inbox(self, path: Path) -> bool:
        return path.resolve().parent == self.inbox.directory.resolve()

    # ──────────────────────────────── отчёт ────────────────────────────────

    def _report(self, extraction: Extraction) -> None:
        self.console.say()
        self.console.step(
            f"Разобрано проводок: {extraction.transactions}, "
            f"сочтено дубликатами: {extraction.duplicates}"
        )
        if not extraction.uncategorized:
            return

        self.console.warn(f"Без категории: {len(extraction.uncategorized)}")
        for txn in extraction.uncategorized[:SHOWN]:
            self.console.say(f"  {self._line(txn)}")
        if len(extraction.uncategorized) > SHOWN:
            self.console.say(f"  … и ещё {len(extraction.uncategorized) - SHOWN}")
        self.console.say(
            "Их можно перенести как есть и разметить потом, а можно дописать "
            "правило в rules.yaml и запустить снова."
        )

    @staticmethod
    def _line(txn: data.Transaction) -> str:
        """Одна проводка в одну строку: дата, кто, сколько."""
        units = txn.postings[0].units if txn.postings else None
        amount = f"{units.number:>12,.2f} {units.currency}" if units else ""
        who = txn.payee or txn.narration or ""
        return f"{txn.date} {who[:40]:<40} {amount}"

    def _finish(self, steps: list[Callable[[], Step]], skipped: list[Path]) -> int:
        self.console.say()
        for run_step in steps:
            step = run_step()
            if step.failed:
                self.console.fail(f"{step.name}: не получилось")
                if step.output:
                    self.console.say(step.output)
                return 1
            self.console.ok(f"{step.name}: готово")

        if skipped:
            names = ", ".join(path.name for path in skipped)
            self.console.warn(f"Осталось разобрать руками: {names}")
        self.console.say(f"Отчёты: fava {self.pipeline.main}")
        return 0


@click.command("add")
@click.argument("src", nargs=-1, type=click.Path(path_type=Path))
@click.option("--copy", "copy_files", is_flag=True, help="не удалять исходные файлы")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="не спрашивать про перенос")
@click.option(
    "--replace",
    is_flag=True,
    help="перезаписать файлы года целиком; нужен после правки rules.yaml",
)
def add(src: tuple[Path, ...], copy_files: bool, assume_yes: bool, replace: bool) -> None:
    """Положить выписки в inbox и прогнать импорт целиком.

    SRC — файлы или папки с выгруженными выписками. Без аргументов разбирается
    то, что уже лежит в inbox.
    """
    rules = Rules.load(RULES)
    inbox = Inbox.build(rules)
    pipeline = Pipeline(inbox=inbox.directory, uncategorized=rules.default_account)
    command = AddCommand(Console(), inbox, pipeline)

    sources = [Path(path) for path in src] or [inbox.directory]
    code = command.run(
        sources, move=not copy_files, assume_yes=assume_yes, replace=replace
    )
    raise SystemExit(code)
