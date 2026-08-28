#!/usr/bin/env python3
"""Собрать анонимизированные тестовые выписки ACBA из настоящих.

Запускается руками, когда меняется формат банка:

    python tools/make_acba_fixtures.py "C:/путь/к/папке/с/выписками"

Ячейки .xls копируются из реальной выписки как есть — так макет в фикстуре
остаётся банковским, а не придуманным. Берутся шапка, блок итогов, заголовки
таблицы и по одной строке на каждый вид операции.

Персональные данные заменяются по карте из tools/anonymize.yaml (её самой в
репозитории нет). Всё, что похоже на идентификатор и в карте не описано,
обрушит сборку — см. LEAK_RE в tools/anonymize.py.
"""

from __future__ import annotations

import io
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import xlrd
import xlwt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Импорты ниже — после вставки в sys.path: без неё их не найти.
# pylint: disable=wrong-import-position
from tools.anonymize import Anonymizer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DST = ROOT / "tests" / "acba"


# ───────────────────────────── карта: .xls ─────────────────────────────

#: Строки шапки, итогов и двухуровневого заголовка таблицы — копируются целиком.
HEADER_ROWS = 21

#: По одной строке на каждый вид операции: (подстрока в описании, сколько брать).
CARD_SAMPLES = [
    ("Card service fee", 1),
    ("Debit from card account", 1),
    ("Electronic payment", 2),  # с мерчантом
    ("Reversal", 1),  # возврат, сумма в кредит
    ("Transfer", 1),
    ("Credit to card account", 1),
    ("Card to card transfer", 2),
    ("Cash Withdrawal", 1),
    ("Purchase", 1),
]

#: Блок «TRANSACTION SUMMARY» — обороты по счёту за период. Настоящие суммы
#: показывают масштаб трат семьи, поэтому заменяются на круглые выдуманные.
#: Ключ — начало подписи в строке заголовков, подписи многострочные.
SUMMARY_VALUES = {
    "Initial balance": "10,000.00",
    "Credits": "+ 500,000.00",
    "Debits": "- 490,000.00",
    "Fees": "- 1,000.00",
    "Fine": "0.00",
    "Interest": "0.00",
    "Final balance": "20,000.00",
}

AVAILABLE_RE = re.compile(r"(Available balance\s+)[\d,.]+")

COL_DATE, COL_DESCRIPTION = 1, 26
DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def cell_text(sheet, row: int, col: int) -> str:
    if row >= sheet.nrows or col >= sheet.ncols:
        return ""
    cell = sheet.cell(row, col)
    if cell.ctype == xlrd.XL_CELL_EMPTY:
        return ""
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        return repr(cell.value)
    return str(cell.value).strip()


def summary_overrides(sheet) -> dict[tuple[int, int], str]:
    """Подменить обороты в блоке итогов, не трогая макет.

    Значения лежат в строке под подписями и в тех же колонках, так что
    достаточно найти строку подписей.
    """
    overrides: dict[tuple[int, int], str] = {}
    for row in range(HEADER_ROWS):
        for col in range(min(sheet.ncols, 256)):
            text = cell_text(sheet, row, col)
            for label, value in SUMMARY_VALUES.items():
                if text.startswith(label):
                    overrides[(row + 1, col)] = value
            if text.startswith("Available balance"):
                overrides[(row, col)] = AVAILABLE_RE.sub(
                    r"\g<1>" + SUMMARY_VALUES["Final balance"], text
                )
    return overrides


def build_card_fixture(anon: Anonymizer, source: Path, out: Path) -> int:
    """Собрать фикстуру карточной выписки (.xls). Возвращает число строк.

    Ячейки копируются как есть, чтобы макет остался банковским; подменяется
    только содержимое.
    """
    sheet = xlrd.open_workbook(source, logfile=io.StringIO()).sheet_by_index(0)

    picked: list[int] = []
    for needle, count in CARD_SAMPLES:
        found = 0
        for row in range(HEADER_ROWS, sheet.nrows):
            if row in picked or not DATE_RE.match(cell_text(sheet, row, COL_DATE)):
                continue
            if needle not in cell_text(sheet, row, COL_DESCRIPTION):
                continue
            picked.append(row)
            found += 1
            if found == count:
                break
        if not found:
            print(f"  ! не нашлось строк для {needle!r}")
    # Одна строка с остатком на конец дня — парсер обязан её пропустить.
    for row in range(HEADER_ROWS, sheet.nrows):
        if cell_text(sheet, row, COL_DESCRIPTION).startswith("Balance-"):
            picked.append(row)
            break
    picked.sort()

    overrides = summary_overrides(sheet)

    book = xlwt.Workbook(encoding="utf-8")
    out_sheet = book.add_sheet("Statement")
    for index, row in enumerate([*range(HEADER_ROWS), *picked]):
        for col in range(min(sheet.ncols, 256)):
            text = overrides.get((row, col), cell_text(sheet, row, col))
            if not text:
                continue
            text = anon.scrub(text)
            anon.check(text, f"{out.name}, строка {row + 1}, колонка {col + 1}")
            out_sheet.write(index, col, text)
    book.save(str(out))
    return len(picked)


# ───────────────────────────── счёт: .xml ─────────────────────────────

#: По одной операции каждого типа плюс пара переводов с контрагентами.
ACCOUNT_TYPES = {"TRF": 3, "CEX": 2, "FEE": 1, "MSC": 2}


def build_account_fixture(anon: Anonymizer, source: Path, out: Path) -> int:
    """Собрать фикстуру выписки по счёту (.xml). Возвращает число операций."""
    root = ET.parse(source).getroot()
    node = root.find("Transactions")

    taken: list[ET.Element] = []
    left = dict(ACCOUNT_TYPES)
    for row in node.findall("Transaction"):
        kind = row.get("OPERATIONTYPE")
        if left.get(kind):
            left[kind] -= 1
            taken.append(row)

    # Номера операций — настоящие банковские идентификаторы, но перечислять их
    # в карте замен бессмысленно. Перенумеровываем, сохраняя совпадения: у
    # перевода и удержанной за него комиссии номер общий, и на этом держится
    # тест дедупа.
    renumbered: dict[str, str] = {}
    for row in taken:
        old = row.get("TRANSACTIONID")
        if old and old not in renumbered:
            renumbered[old] = str(400000000000 + len(renumbered) + 1)

    for element in [root, node, *taken]:
        for key, value in list(element.attrib.items()):
            if key == "TRANSACTIONID":
                element.set(key, renumbered.get(value, value))
                continue
            value = anon.scrub(value)
            anon.check(value, f"{out.name}, атрибут {key}")
            element.set(key, value)

    for row in list(node):
        node.remove(row)
    for row in taken:
        node.append(row)

    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    return len(taken)


if __name__ == "__main__":
    anon = Anonymizer.load()
    source = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "inbox")
    # По папке на импортёр: beangulp `generate` считает ошибкой файл, который
    # проверяемый импортёр не опознаёт, а карту и счёт читают разные классы.
    (DST / "card").mkdir(parents=True, exist_ok=True)
    (DST / "account").mkdir(parents=True, exist_ok=True)

    n = build_card_fixture(anon, source / "amd_card.xls", DST / "card" / "card.xls")
    print(f"card/card.xls: {n} операций")
    n = build_account_fixture(anon, source / "amd_account.xml", DST / "account" / "account.xml")
    print(f"account/account.xml: {n} операций")
