"""Импортёр выписок ACBA Bank (Армения).

Банк отдаёт по каждому счёту два файла, и они дополняют друг друга — поэтому
здесь два парсера, а не один:

* `*_card.xls` — **карточные счета**. Есть название мерчанта (`GLOVO APP`,
  `OPENAI *CHATGPT SUBSCR`), валюта операции, курс и комиссия. В xml по этим же
  операциям вместо мерчанта стоит `Համ ARCA-ի քաղվ.(<номер>)`, то есть
  ничего пригодного для учёта трат.

* `*_account.xml` — **обычные счета**. Есть контрагент, номер его счёта,
  осмысленный комментарий и `TRANSACTIONID`. В xls по этим счетам контрагента
  нет ни в одной строке, а треть строк вообще без описания.

Общее для обоих: номер счёта лежит внутри файла, поэтому раскладывать выписки
по папкам, как для Ameriabank, не нужно — импортёр опознаёт свой файл сам.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import xml.etree.ElementTree as ET
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

import beangulp
import xlrd
from beancount.core import amount as bc_amount
from beancount.core import data, flags
from beangulp import extract

from finance.booking import categorize
from finance.categorize import Rules
from finance.importers import importer_name

#: ACBA обозначает рубль устаревшим кодом. beancount ожидает ISO 4217.
CURRENCY_ALIASES = {"RUR": "RUB"}

#: Номер счёта в шапке: `Account number: 100000000000001`.
ACCOUNT_NUMBER_RE = re.compile(r"Account\s+number\D*(\d{6,})", re.IGNORECASE)

#: Период выписки: `Statement Period.  01.01.2026 - 21.08.2026 (233) days`.
PERIOD_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})")

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def _decimal(text: str) -> Decimal:
    """Разобрать сумму вида `- 4,996.10`, `+ 845.30`, `0.00` или пустую строку."""
    cleaned = re.sub(r"[\s,]", "", text or "")
    if not cleaned:
        return Decimal(0)
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"не разобрать сумму {text!r}") from exc


def _currency(code: str) -> str:
    return CURRENCY_ALIASES.get(code.strip().upper(), code.strip().upper())


# ─────────────────────────── карточные счета: xls ───────────────────────────

# Колонки таблицы операций (нумерация с нуля). Шапка занимает две строки:
# в 19-й строке группы, в 20-й — подписи Credit/Debit внутри группы.
COL_DATE = 1
COL_TXN_AMOUNT = 5
COL_TXN_CURRENCY = 7
COL_CREDIT = 8
COL_DEBIT = 13
COL_RATE = 17
COL_SETTLEMENT = 18
COL_BALANCE = 21
COL_DESCRIPTION = 26
COL_PLACE = 29
COL_FEE = 33
COL_CARD = 35

#: Строка с остатком на конец дня, а не операция.
BALANCE_MARKER = "Balance-"

#: Куда относить комиссию, удержанную за карточную операцию.
FEE_ACCOUNT = "Expenses:Fees:Bank:Card"

#: Так банк помечает операцию без комиссии.
NO_FEE = "not applicable"


def _fee(text: str) -> Decimal:
    return Decimal(0) if not text or text == NO_FEE else _decimal(text)


def _fee_posting(value: Decimal, currency: str) -> data.Posting:
    return data.Posting(FEE_ACCOUNT, bc_amount.Amount(value, currency), None, None, None, None)


class CardImporter(beangulp.Importer):
    """ACBA card account statement (.xls)."""

    def __init__(self, account: str, currency: str, number: str, rules: Rules):
        self.importer_account = account
        self.currency = currency
        self.number = number
        self.rules = rules

    @property
    def name(self) -> str:
        return importer_name("acba", self.importer_account)

    def identify(self, filepath: str) -> bool:
        if Path(filepath).suffix.lower() != ".xls":
            return False
        sheet = _open_sheet(filepath)
        if sheet is None or sheet.name != "Statement":
            return False
        return _find_account_number(sheet) == self.number

    def account(self, filepath: str) -> str:
        return self.importer_account

    def date(self, filepath: str) -> dt.date | None:
        _, end = _statement_period(_open_sheet(filepath))
        return end

    def filename(self, filepath: str) -> str:
        return f"acba-{self.currency.lower()}-card.xls"

    def extract(self, filepath: str, existing: data.Entries) -> data.Entries:
        sheet = _open_sheet(filepath)
        if sheet is None:
            return []

        entries: data.Entries = []
        for row in range(sheet.nrows):
            date_text = _cell(sheet, row, COL_DATE)
            if not DATE_RE.match(date_text):
                continue
            entries.append(self._transaction(filepath, row, sheet))

        balance = self._balance(filepath, sheet)
        if balance is not None:
            entries.append(balance)
        return entries

    def _transaction(self, filepath: str, row: int, sheet) -> data.Transaction:
        # Приход и расход — разные колонки, заполнена всегда ровно одна.
        value = _decimal(_cell(sheet, row, COL_CREDIT)) + _decimal(_cell(sheet, row, COL_DEBIT))
        # Комиссия лежит в своей колонке и в Credit/Debit НЕ входит: со счёта
        # уходит сумма операции плюс она. Без отдельной ноги остаток по карте
        # расходится с банковским ровно на сумму всех комиссий.
        fee = _fee(_cell(sheet, row, COL_FEE))

        meta = data.new_metadata(filepath, row + 1)
        settlement = _cell(sheet, row, COL_SETTLEMENT)
        if settlement:
            meta["settlement"] = settlement
        # Покупка в чужой валюте: сколько и по какому курсу списал банк.
        txn_currency = _currency(_cell(sheet, row, COL_TXN_CURRENCY))
        if txn_currency and txn_currency != self.currency:
            meta["original"] = f"{_cell(sheet, row, COL_TXN_AMOUNT)} {txn_currency}"
            rate = _cell(sheet, row, COL_RATE)
            if rate:
                meta["rate"] = rate
        card = _cell(sheet, row, COL_CARD)
        if card and card != "not applicable":
            meta["card"] = card

        txn = data.Transaction(
            meta,
            _parse_date(_cell(sheet, row, COL_DATE)),
            flags.FLAG_OKAY,
            None,
            "",
            frozenset(),
            frozenset(),
            [
                data.Posting(
                    self.importer_account,
                    # Со счёта ушла операция вместе с комиссией.
                    bc_amount.Amount(value + fee, self.currency),
                    None,
                    None,
                    None,
                    None,
                )
            ],
        )
        txn = categorize(
            txn,
            self.rules,
            # Мерчант приходит выровненным пробелами и с запятой на конце.
            counterparty=_cell(sheet, row, COL_PLACE).rstrip(", ").strip(),
            details=_cell(sheet, row, COL_DESCRIPTION),
            txn_type="card",
            # Категорию подбираем по сумме самой операции, без комиссии.
            amount=value,
        )
        if not fee:
            return txn
        # Нога с суммой ставится перед категорией: у категории суммы нет,
        # beancount выводит её сам, и такая нога в проводке может быть одна.
        postings = [*txn.postings[:-1], _fee_posting(-fee, self.currency), txn.postings[-1]]
        return txn._replace(postings=postings)

    def _balance(self, filepath: str, sheet) -> data.Balance | None:
        """Сверка с итоговым остатком из шапки выписки.

        Ловит расхождение разбора с тем, что насчитал сам банк.
        """
        final = _final_balance(sheet)
        _, end = _statement_period(sheet)
        if final is None or end is None:
            return None
        # Balance проверяет остаток на начало дня — берём следующий день.
        return data.Balance(
            data.new_metadata(filepath, 0),
            end + dt.timedelta(days=1),
            self.importer_account,
            bc_amount.Amount(final, self.currency),
            None,
            None,
        )


def _open_sheet(filepath: str):
    # ACBA формирует .xls с обрезанным последним сектором, и xlrd печатает об
    # этом предупреждение прямо в stdout, ломая вывод extract. Файл при этом
    # читается полностью, поэтому просто уводим её лог в никуда.
    try:
        book = xlrd.open_workbook(filepath, logfile=io.StringIO())
        return book.sheet_by_index(0)
    except (xlrd.XLRDError, OSError, IndexError):
        return None


def _cell(sheet, row: int, col: int) -> str:
    if row >= sheet.nrows or col >= sheet.ncols:
        return ""
    cell = sheet.cell(row, col)
    if cell.ctype == xlrd.XL_CELL_EMPTY:
        return ""
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        return repr(cell.value)
    return str(cell.value).strip()


def _header_cells(sheet, rows: int = 20):
    for row in range(min(rows, sheet.nrows)):
        for col in range(sheet.ncols):
            text = _cell(sheet, row, col)
            if text:
                yield row, col, text


def _find_account_number(sheet) -> str | None:
    for _, _, text in _header_cells(sheet, rows=8):
        found = ACCOUNT_NUMBER_RE.search(text)
        if found:
            return found.group(1)
    return None


def _statement_period(sheet) -> tuple[dt.date | None, dt.date | None]:
    if sheet is None:
        return None, None
    for _, _, text in _header_cells(sheet, rows=8):
        if "Statement Period" not in text and "REPORT PERIOD" not in text:
            continue
        found = PERIOD_RE.search(text)
        if found:
            return _parse_date(found.group(1)), _parse_date(found.group(2))
    return None, None


def _final_balance(sheet) -> Decimal | None:
    """Итоговый остаток из блока «TRANSACTION SUMMARY» над таблицей операций."""
    for row, col, text in _header_cells(sheet):
        if text.startswith("Final balance"):
            value = _cell(sheet, row + 1, col)
            return _decimal(value) if value else None
    return None


def _parse_date(text: str) -> dt.date:
    return dt.datetime.strptime(text, "%d.%m.%Y").date()


# ──────────────────────────── обычные счета: xml ────────────────────────────


class AccountImporter(beangulp.Importer):
    """ACBA current/saving account statement (.xml)."""

    def __init__(self, account: str, currency: str, number: str, rules: Rules):
        self.importer_account = account
        self.currency = currency
        self.number = number
        self.rules = rules

    @property
    def name(self) -> str:
        return importer_name("acba", self.importer_account)

    def identify(self, filepath: str) -> bool:
        if Path(filepath).suffix.lower() != ".xml":
            return False
        node = _transactions_node(filepath)
        return node is not None and node.get("ACCOUNTNO") == self.number

    def account(self, filepath: str) -> str:
        return self.importer_account

    def date(self, filepath: str) -> dt.date | None:
        node = _transactions_node(filepath)
        if node is None or not node.get("TODATE"):
            return None
        return dt.datetime.strptime(node.get("TODATE"), "%d/%m/%Y").date()

    def filename(self, filepath: str) -> str:
        return f"acba-{self.currency.lower()}-account.xml"

    def extract(self, filepath: str, existing: data.Entries) -> data.Entries:
        node = _transactions_node(filepath)
        if node is None:
            return []

        rows = list(node.findall("Transaction"))
        # TRANSACTIONID не уникален: перевод и удержанная за него комиссия
        # приходят с одним и тем же номером. Добавляем порядковый номер внутри
        # группы, чтобы ключ дедупа всё-таки был однозначным.
        seen: Counter[str] = Counter()
        entries: data.Entries = []
        for index, row in enumerate(rows):
            txn_id = row.get("TRANSACTIONID", "")
            seen[txn_id] += 1
            # Номер счёта в ключе обязателен: у перевода между своими счетами
            # обе ноги приходят с ОДНИМ TRANSACTIONID, каждая в своей выписке.
            # Без него нога второго счёта считается дубликатом первой и теряется.
            key = f"{self.number}:{txn_id}-{seen[txn_id]}"
            entries.append(self._transaction(filepath, index, row, key))
        return entries

    def _transaction(self, filepath: str, index: int, row, key: str) -> data.Transaction:
        credit = _decimal(row.get("CREDITAMOUNT", "0"))
        debit = _decimal(row.get("DEBITAMOUNT", "0"))
        value = credit - debit

        meta = data.new_metadata(filepath, index + 1)
        meta["acba-id"] = key
        if row.get("OPERATIONTYPE"):
            meta["bank-type"] = row.get("OPERATIONTYPE")
        # Номер счёта контрагента: по нему видно, что перевод ушёл на другой
        # свой счёт, и с чем сводить транзитный остаток.
        if row.get("CORRESPONDENTACCOUNT"):
            meta["correspondent"] = row.get("CORRESPONDENTACCOUNT")
        # Эквивалент в драмах — для валютных счетов это фактический курс сделки.
        in_amd = _decimal(row.get("CREDITAMOUNTAMD", "0")) - _decimal(row.get("DEBITAMOUNTAMD", "0"))
        if self.currency != "AMD" and in_amd:
            meta["amd"] = f"{in_amd} AMD"

        txn = data.Transaction(
            meta,
            dt.datetime.strptime(row.get("OPERATIONDATE"), "%d/%m/%Y").date(),
            flags.FLAG_OKAY,
            None,
            "",
            frozenset(),
            frozenset(),
            [
                data.Posting(
                    self.importer_account,
                    bc_amount.Amount(value, self.currency),
                    None,
                    None,
                    None,
                    None,
                )
            ],
        )
        return categorize(
            txn,
            self.rules,
            counterparty=(row.get("CORRESPONDENTNAME") or "").strip(),
            details=(row.get("COMMENT") or "").strip(),
            txn_type=(row.get("OPERATIONTYPE") or "").strip(),
            amount=value,
            correspondent=(row.get("CORRESPONDENTACCOUNT") or "").strip(),
        )

    def deduplicate(self, entries: data.Entries, existing: data.Entries) -> None:
        """Точный дедуп по номеру операции вместо эвристики по дате и сумме.

        Эвристика тут опасна: по рублёвому счёту 133 из 171 операции — это
        «Currency exchange regulation», среди них полно одинаковых сумм в
        соседние дни. Банк даёт настоящий идентификатор — пользуемся им.
        """
        known = {
            entry.meta["acba-id"]: entry
            for entry in existing
            if isinstance(entry, data.Transaction) and "acba-id" in entry.meta
        }
        for entry in entries:
            match = known.get(entry.meta.get("acba-id"))
            if match is not None:
                entry.meta[extract.DUPLICATE] = match


def _transactions_node(filepath: str):
    try:
        root = ET.parse(filepath).getroot()
    except (ET.ParseError, OSError):
        return None
    return root.find("Transactions") if root.tag != "Transactions" else root