#!/usr/bin/env python3
"""Перенести вывод `import.py extract` в леджер, разложив по годам.

    python tools/merge_extract.py out.beancount
    python tools/merge_extract.py out.beancount --replace

Что делает:

* берёт из файла только живые директивы — строки, закомментированные beangulp
  как дубликаты, парсер и так не видит;
* раскладывает их по `ledger/transactions/<год>.beancount`;
* сливает с тем, что уже лежит в файле, и сортирует всё по дате, чтобы леджер
  оставался хронологическим независимо от порядка импорта;
* повторный запуск на том же файле ничего не добавляет.

Про повторный запуск: одинаковые проводки считаются по количеству, а не по
факту наличия. Четыре одинаковые SMS-комиссии в одну минуту — это четыре
разные операции, и если в леджере их уже четыре, добавится ноль, а если две,
добавятся ещё две.

`--replace` перезаписывает файлы года целиком. Нужен после правки rules.yaml:
у проводок меняется счёт категории, обычный перенос считает их новыми и
задвоит. Правки руками в затронутых файлах при этом теряются.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from beancount.core import data
from beancount.parser import parser, printer

ROOT = Path(__file__).resolve().parents[1]
TRANSACTIONS = ROOT / "ledger" / "transactions"

HEADER = """\
;; Проводки за {year} год.
;;
;; ОТКРЫВАТЬ НУЖНО НЕ ЭТОТ ФАЙЛ, А ledger/main.beancount:
;;
;;     fava ledger/main.beancount
;;     bean-check ledger/main.beancount
;;
;; Здесь лежат только проводки — без плана счетов, опций и начальных остатков.
;; Сам по себе файл невалиден: beancount выдаст ошибку на каждую ссылку на
;; счёт, который нигде не открыт, — несколько тысяч штук.
;;
;; Файл собирается автоматически: python tools/merge_extract.py out.beancount
;; Правки руками сохранятся — инструмент дописывает недостающее и пересортирует,
;; но комментарии между проводками потеряются. С --replace теряются и правки.
"""


def identity(entry: data.Directive) -> tuple:
    """Ключ «та же самая операция» для сравнения с уже перенесённым.

    Намеренно грубый: дата, участники и суммы. Метаданные не берём — они могут
    отличаться путём к файлу выписки, из которого проводка приехала.
    """
    if isinstance(entry, data.Transaction):
        return (
            "txn",
            entry.date,
            entry.flag,
            entry.payee,
            entry.narration,
            tuple(
                (p.account, str(p.units) if p.units else None) for p in entry.postings
            ),
        )
    if isinstance(entry, data.Balance):
        return ("balance", entry.date, entry.account, str(entry.amount))
    return ("other", entry.date, printer.format_entry(entry))


def load(path: Path) -> list[data.Directive]:
    if not path.exists():
        return []
    entries, errors, _ = parser.parse_file(str(path))
    fatal = [e for e in errors if "duplicate" not in e.message.lower()]
    if fatal:
        for error in fatal[:5]:
            print(f"  ! {path.name}: {error.message}", file=sys.stderr)
        raise SystemExit(f"{path} не разбирается, перенос отменён")
    return entries


def main(source: Path, replace: bool = False) -> None:
    incoming = load(source)
    if not incoming:
        raise SystemExit(f"{source}: нечего переносить")

    by_year: dict[int, list[data.Directive]] = {}
    for entry in incoming:
        by_year.setdefault(entry.date.year, []).append(entry)

    TRANSACTIONS.mkdir(parents=True, exist_ok=True)
    for year, entries in sorted(by_year.items()):
        target = TRANSACTIONS / f"{year}.beancount"
        existing = [] if replace else load(target)

        # Считаем по количеству: одинаковых операций может быть несколько,
        # и схлопывать их до одной нельзя.
        have = Counter(identity(e) for e in existing)
        added = []
        for entry in entries:
            key = identity(entry)
            if have[key]:
                have[key] -= 1
                continue
            added.append(entry)

        merged = sorted([*existing, *added], key=data.entry_sortkey)
        text = HEADER.format(year=year) + "\n"
        text += "\n".join(printer.format_entry(e) for e in merged)
        target.write_text(text, encoding="utf-8")

        print(
            f"{target.relative_to(ROOT)}: +{len(added)} новых, "
            f"{len(merged)} всего ({len(entries) - len(added)} уже были)"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser_ = argparse.ArgumentParser(
        description="Перенести вывод `import.py extract` в ledger/transactions/, разложив по годам.",
        epilog=(
            "Повторный запуск на том же файле ничего не добавляет: одинаковые "
            "проводки сравниваются по количеству, а не по факту наличия."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_.add_argument(
        "source",
        type=Path,
        help="файл, полученный из `import.py extract -o`",
    )
    parser_.add_argument(
        "--replace",
        action="store_true",
        help=(
            "перезаписать файлы года целиком вместо слияния. Нужен после правки "
            "rules.yaml: у проводок меняется счёт категории, обычный перенос "
            "считает их новыми и задвоит. Правки руками при этом теряются"
        ),
    )
    return parser_.parse_args(argv)


if __name__ == "__main__":
    options = parse_args()
    main(options.source, replace=options.replace)