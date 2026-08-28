"""Цепочка импорта одной командой: разобрать, перенести, обновить курсы, убрать.

Ровно те шаги, что расписаны в README по пунктам, — и ровно теми же командами.
Шаги, у которых есть свой CLI, и запускаются своим CLI, подпроцессом: так они
делают буквально то же самое, что сделал бы человек руками, и README не может
разойтись с кодом. Отчёт при этом собирается не из их вывода, а разбором
out.beancount: считать по печатному тексту — значит сломаться от первой же
правки формулировки.

Проверка леджера — единственный шаг без подпроцесса: bean-check это тонкая
обёртка над beancount.loader, и звать её через процесс только ради того, чтобы
потом разбирать её вывод, незачем.

Как и finance/inbox.py, модуль ничего не спрашивает и ничего не печатает —
решает вызывающий.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from beancount import loader
from beancount.core import data
from beancount.parser import parser

from finance import config

ROOT = Path(__file__).resolve().parents[1]

#: Закомментированная beangulp проводка-дубликат: он отбивает весь блок `; `,
#: и первой строкой блока идёт дата с флагом. Живые проводки так не выглядят,
#: поэтому счёт по этой строке точный.
DUPLICATE_RE = re.compile(r"^; \d{4}-\d{2}-\d{2} [*!]", re.MULTILINE)


@dataclass
class Step:
    """Результат одного шага цепочки."""

    name: str
    ok: bool
    output: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok


@dataclass
class Extraction(Step):
    """Что получилось у `import.py extract`."""

    path: Path | None = None
    transactions: int = 0
    duplicates: int = 0
    #: Проводки, под которые не подошло ни одно правило. Их и смотрят глазами.
    uncategorized: list[data.Transaction] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """Разбирать нечего: всё, что было в inbox, уже в леджере."""
        return self.transactions == 0


class Pipeline:
    """Шаги импорта поверх готового inbox.

    Раскладкой файлов занимается finance/inbox.py — сюда они приезжают уже
    названными правильно.
    """

    def __init__(
        self,
        *,
        inbox: Path | None = None,
        ledger: Path | None = None,
        documents: Path | None = None,
        out: Path | None = None,
        uncategorized: str = "Expenses:Uncategorized",
    ) -> None:
        self.inbox = inbox or config.INBOX
        self.ledger = ledger or config.LEDGER
        self.documents = documents or config.DOCUMENTS
        self.out = out or ROOT / "out.beancount"
        self.uncategorized = uncategorized

    @property
    def main(self) -> Path:
        """Корневой файл леджера — тот, что открывают fava и bean-check."""
        return self.ledger / "main.beancount"

    # ─────────────────────────────── шаги ───────────────────────────────

    def extract(self) -> Extraction:
        """Разобрать всё, что лежит в inbox, в out.beancount.

        Существующий леджер показывается импортёрам ключом `-e`: без него они
        не узнают уже перенесённые проводки и разметят дубликаты как новые.
        Леджера может не быть — он в приватном репозитории, и без него импорт
        всё равно работает, просто без дедупликации.
        """
        command = ["extract", str(self.inbox), "-o", str(self.out)]
        if self.main.exists():
            command += ["-e", str(self.main)]

        step = self._run("Разбор выписок", command)
        result = Extraction(step.name, step.ok, step.output, path=self.out)
        if step.failed or not self.out.exists():
            return result

        text = self.out.read_text(encoding="utf-8")
        entries, _, _ = parser.parse_string(text)
        result.duplicates = len(DUPLICATE_RE.findall(text))
        transactions = [e for e in entries if isinstance(e, data.Transaction)]
        result.transactions = len(transactions)
        result.uncategorized = [
            txn
            for txn in transactions
            if any(posting.account == self.uncategorized for posting in txn.postings)
        ]
        return result

    def merge(self, *, replace: bool = False) -> Step:
        """Перенести разобранное в леджер, разложив по годам."""
        command = [str(ROOT / "tools" / "merge_extract.py"), str(self.out)]
        if replace:
            command.append("--replace")
        return self._python("Перенос в леджер", command)

    def rates(self) -> Step:
        """Обновить курсы ЦБ Армении: без них доход в рублях не приводится к драму."""
        return self._python("Курсы валют", [str(ROOT / "tools" / "fetch_rates.py")])

    def archive(self) -> Step:
        """Убрать разобранные выписки из inbox в documents."""
        return self._run(
            "Архив выписок", ["archive", str(self.inbox), "-o", str(self.documents)]
        )

    def check(self) -> Step:
        """Убедиться, что леджер валиден. Это bean-check, только без подпроцесса."""
        if not self.main.exists():
            return Step("Проверка леджера", True, f"{self.main} не найден, пропущено")

        _, errors, _ = loader.load_file(str(self.main))
        if not errors:
            return Step("Проверка леджера", True)
        shown = "\n".join(
            f"{error.source.get('filename', '?')}:{error.source.get('lineno', '?')}: "
            f"{error.message}"
            for error in errors[:20]
        )
        if len(errors) > 20:
            shown += f"\n… и ещё {len(errors) - 20}"
        return Step("Проверка леджера", False, shown)

    # ────────────────────────────── запуск ──────────────────────────────

    def _run(self, name: str, command: list[str]) -> Step:
        """Позвать import.py — тот же CLI, что описан в README."""
        return self._python(name, [str(ROOT / "import.py"), *command])

    def _python(self, name: str, command: list[str]) -> Step:
        completed = subprocess.run(  # noqa: S603
            [sys.executable, *command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        return Step(name, completed.returncode == 0, output.strip())
