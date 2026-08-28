"""Импортёр «Индивидуальной выписки по платёжному счёту» Сбербанка (Россия).

Формат — PDF, как и у Т-Банка, и разбирается так же: по координатам слов, а не
по разделителям. Дальше начинаются различия, и почти каждое из них меняет код.

  * **одна операция занимает ровно две строки.** В первой дата, время, категория
    и сумма; во второй дата обработки, код авторизации и описание. Признак
    начала операции — время во второй колонке;
  * **колонка сумм прижата вправо**, а не влево: у неё нет постоянного левого
    края, поэтому она опознаётся по правой части страницы целиком;
  * **разделитель тысяч — неразрывный пробел, десятичный — запятая**, и так во
    всей выписке, включая итоги. У Т-Банка запятая только в блоке итогов;
  * **знак есть только у прихода.** Расход приходит без минуса: `587,00` — это
    списание, `+535,86` — зачисление;
  * **банк сам проставляет категорию операции** («Прочие расходы», «Перевод
    СБП», «Возврат, отмена операции»). Она уходит в `type` и доступна правилам.

Две вещи, которых у Т-Банка нет вовсе:

  * **код авторизации** — настоящий номер операции, уникальный. На нём и
    держится дедуп, без синтетики из даты и суммы;
  * **комиссия отдельной строкой** под операцией: «В сумму операции включена
    комиссия 3,01 руб.». Слово «включена» тут важное — в отличие от ACBA, где
    комиссия к сумме прибавляется, здесь она уже внутри, и её ногу приходится
    из суммы вычитать, а не добавлять к ней.

Чего в выписке нет: **остатка по счёту в любом виде**. Есть только обороты за
период — с ними и сверяется разбор. Начальный остаток придётся вписать руками,
как для Ameriabank.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import beangulp
import pdfplumber
from beancount.core import amount as bc_amount
from beancount.core import data, flags
from beangulp import extract

from finance.booking import categorize
from finance.categorize import Rules
from finance.importers import importer_name

#: Левые края колонок. Последняя — не край, а граница: суммы прижаты к правому
#: полю страницы, и левый край у них гуляет вместе с длиной числа. Описание
#: правее 393 не заходит, шапка колонки сумм начинается на 469 — граница между.
COLUMNS = (44, 95, 144, 460)
COL_DATE, COL_TIME, COL_TEXT, COL_AMOUNT = range(len(COLUMNS))

#: На сколько пунктов ячейки одной строки могут разъезжаться по вертикали.
#: Строки внутри операции отстоят на 11, между операциями — на 17.
ROW_TOLERANCE = 3.0

#: Заголовок документа — подпись формата в `identify()`.
TITLE = "Индивидуальная выписка по платёжному счёту"

#: Первая ячейка шапки таблицы. Шапка занимает три строки и повторяется на
#: каждой странице.
TABLE_HEADER = "ДАТА ОПЕРАЦИИ"
TABLE_HEADER_LINES = 3

#: На чём таблица заканчивается: перенос на следующую страницу, блок проверки
#: подлинности и реквизиты документа на последней странице.
STOP_MARKERS = (
    "Продолжение на следующей",
    "Для проверки подлинности",
    "Дата формирования документа",
)

#: Номер счёта банк печатает группами цифр через пробел: `10000 000 0 0000 0000003`.
ACCOUNT_NUMBER_RE = re.compile(r"Номер счёта\s+((?:\d+\s+)*\d+)")
PERIOD_RE = re.compile(r"За период\s+(\d{2}\.\d{2}\.\d{4})\s*[—–-]\s*(\d{2}\.\d{2}\.\d{4})")
CURRENCY_RE = re.compile(r"Валюта\s+(\S+(?: \S+)?)")

#: Тысячи Сбербанк разделяет НЕРАЗРЫВНЫМ пробелом, и во всех регулярках ниже он
#: записан escape-последовательностью намеренно: буквальный U+00A0 в исходнике
#: неотличим от обычного пробела, и правка «лишнего» пробела сломала бы разбор
#: молча. Внутри класса символов escape понимает сам модуль re.
TOTAL_RES = {
    "credit": re.compile(r"Пополнение\s+\+?([\d\u00a0 ]+,\d{2})"),
    "debit": re.compile(r"Списание\s+\+?([\d\u00a0 ]+,\d{2})"),
}

#: Комиссия, уже включённая в сумму операции.
FEE_RE = re.compile(r"^В сумму операции включена комиссия\s+([\d\u00a0 ]+,\d{2})\s*руб\.?$")
FEE_ACCOUNT = "Expenses:Fees:Bank:Transfer"

#: Хвост описания: `. Операция по карте ****0001` / `. Операция по счету ****0003`.
#: Повторяется в каждой строке и ничего не добавляет — уезжает в метаданные.
TAIL_RE = re.compile(r"\.\s*Операция по (карте|счету)\s+(\*+\d+)\s*$")

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")

#: Сумма: `+2 050 679,78`, `3 844,00`. Неразрывный пробел разделяет тысячи,
#: запятая — копейки, плюс есть только у прихода.
MONEY_RE = re.compile(r"^(\+?)\s*([\d\u00a0 ]+),(\d{2})$")

#: Валюту банк пишет словами.
CURRENCY_NAMES = {"Российский рубль": "RUB"}


class Importer(beangulp.Importer):
    """Sberbank (Russia) account statement (.pdf)."""

    def __init__(self, account: str, currency: str, number: str, rules: Rules):
        self.importer_account = account
        self.currency = currency
        self.number = number
        self.rules = rules

    @property
    def name(self) -> str:
        """Имя обязано быть уникальным: fava отвергает конфиг с повторами."""
        return importer_name("sber", self.importer_account)

    def identify(self, filepath: str) -> bool:
        """Опознать выписку по заголовку и номеру счёта из её шапки.

        Номер лежит в самом файле, поэтому раскладывать выписки по папкам или
        метить имя файла, как для Ameriabank, не нужно.

        Читается только первая страница: `identify()` зовётся для каждого
        импортёра на каждый файл, в том числе на вкладке импорта fava.
        """
        if Path(filepath).suffix.lower() != ".pdf":
            return False
        text = _first_page_text(filepath)
        if text is None or TITLE not in text:
            return False
        found = ACCOUNT_NUMBER_RE.search(text)
        return found is not None and _digits(found.group(1)) == self.number

    def account(self, filepath: str) -> str:
        return self.importer_account

    def date(self, filepath: str) -> dt.date | None:
        """Конец периода выписки. None — beangulp возьмёт дату файла."""
        text = _first_page_text(filepath)
        found = PERIOD_RE.search(text) if text is not None else None
        return _date(found.group(2)) if found else None

    def filename(self, filepath: str) -> str:
        return f"sber-{self.currency.lower()}.pdf"

    def extract(self, filepath: str, existing: data.Entries) -> data.Entries:
        with pdfplumber.open(filepath) as pdf:
            pages = [_rows(page) for page in pdf.pages]
        if not pages:
            return []

        header = _text(pages[0])
        self._check_currency(header, filepath)

        operations: list[_Operation] = []
        for rows in pages:
            operations += _operations(_table_rows(rows))

        # Выписка идёт от новых операций к старым. Порядок задаём явно, а не
        # разворотом списка: сортировка устойчивая, так что совпавшие метки
        # времени сохраняют взаимный порядок — от него зависит нумерация ключа.
        operations.sort(key=lambda op: (op.date, op.time))
        _check_totals(operations, header, filepath)

        seen: dict[str, int] = {}
        return [
            self._transaction(filepath, index, operation, seen)
            for index, operation in enumerate(operations)
        ]

    def _transaction(
        self, filepath: str, index: int, operation: _Operation, seen: dict[str, int]
    ) -> data.Transaction:
        meta = data.new_metadata(filepath, index + 1)
        meta["sber-id"] = _key(self.number, operation, seen)
        if operation.time:
            meta["time"] = operation.time
        # Категория банка — то же поле, что `bank-type` у ACBA и Ameriabank.
        # Правилам она доступна как `type`, а в леджере остаётся видимой.
        if operation.category:
            meta["bank-type"] = operation.category
        # Дата обработки отличается от даты операции у части карточных покупок.
        if operation.processed is not None and operation.processed != operation.date:
            meta["settlement"] = operation.processed.strftime("%Y-%m-%d")
        if operation.card:
            meta["card"] = operation.card
        if operation.original:
            meta["original"] = operation.original

        postings = [
            data.Posting(
                self.importer_account,
                bc_amount.Amount(operation.amount, self.currency),
                None,
                None,
                None,
                None,
            )
        ]
        # Комиссия УЖЕ внутри суммы операции — банк так и пишет: «в сумму
        # операции включена». Поэтому её нога вычитается из суммы, а не
        # добавляется к ней, как у карточных комиссий ACBA. Комиссия всегда
        # расход, поэтому знак у неё положительный независимо от знака операции.
        if operation.fee:
            postings.append(
                data.Posting(
                    FEE_ACCOUNT,
                    bc_amount.Amount(operation.fee, self.currency),
                    None,
                    None,
                    None,
                    None,
                )
            )

        txn = data.Transaction(
            meta,
            operation.date,
            flags.FLAG_OKAY,
            None,
            "",
            frozenset(),
            frozenset(),
            postings,
        )
        return categorize(
            txn,
            self.rules,
            # Отдельной колонки контрагента в выписке нет: имя магазина или
            # получателя лежит в описании. А вот тип операции банк проставляет
            # сам — он и уходит в `type`, правилам он доступен наравне с
            # армянскими типами ACBA.
            counterparty="",
            details=operation.details,
            txn_type=operation.category,
            # Категорию подбираем по сумме самой операции, без комиссии.
            amount=operation.amount + operation.fee,
        )

    def _check_currency(self, header: str, filepath: str) -> None:
        """Убедиться, что валюта выписки совпадает с валютой счёта."""
        found = CURRENCY_RE.search(header)
        if found is None:
            raise ValueError(f"{filepath}: в шапке не нашлась валюта счёта")
        name = found.group(1).strip()
        if CURRENCY_NAMES.get(name) != self.currency:
            raise ValueError(
                f"{filepath}: валюта выписки {name!r} не совпадает с валютой "
                f"счёта {self.currency}; известны {', '.join(CURRENCY_NAMES)}"
            )

    def deduplicate(self, entries: data.Entries, existing: data.Entries) -> None:
        """Точный дедуп по коду авторизации вместо эвристики по дате и сумме.

        Эвристика здесь не нужна: банк даёт настоящий номер операции, и он
        уникален. Окно ±2 дня с допуском 5 % склеило бы одинаковые переводы
        в соседние дни, а их в выписке хватает.
        """
        known = {
            entry.meta["sber-id"]: entry
            for entry in existing
            if isinstance(entry, data.Transaction) and "sber-id" in entry.meta
        }
        for entry in entries:
            match = known.get(entry.meta.get("sber-id"))
            if match is not None:
                entry.meta[extract.DUPLICATE] = match


# ──────────────────────────── разбор страницы ────────────────────────────


@dataclass(frozen=True)
class _Operation:
    """Одна операция, собранная из двух строк таблицы."""

    date: dt.date
    time: str
    #: Дата обработки — когда деньги реально списались или зачислились.
    processed: dt.date | None
    #: Код авторизации: уникальный номер операции.
    auth: str
    #: Категория, которую проставил сам банк.
    category: str
    details: str
    amount: Decimal
    #: Комиссия, уже включённая в сумму операции. Ноль, если её нет.
    fee: Decimal
    card: str
    #: Сумма в валюте операции, если она отличается от валюты счёта.
    original: str


def _column(x: float) -> int | None:
    """Номер колонки, которой принадлежит слово, начинающееся на x."""
    for index in reversed(range(len(COLUMNS))):
        if x >= COLUMNS[index]:
            return index
    return None


def _rows(page) -> list[list[str]]:
    """Строки страницы, разложенные по колонкам таблицы.

    `keep_blank_chars` обязателен: без него описание разрезается по пробелам
    на отдельные слова, а номер счёта в шапке — на группы цифр.
    """
    rows: list[tuple[float, list[str]]] = []
    words = sorted(page.extract_words(keep_blank_chars=True), key=lambda w: (w["top"], w["x0"]))
    for word in words:
        if not rows or word["top"] - rows[-1][0] > ROW_TOLERANCE:
            rows.append((word["top"], [""] * len(COLUMNS)))
        index = _column(word["x0"])
        if index is None:
            continue
        cells = rows[-1][1]
        cells[index] = f"{cells[index]} {word['text']}".strip() if cells[index] else word["text"]
    return [cells for _, cells in rows]


def _text(rows: list[list[str]]) -> str:
    """Страница обычным текстом — для разбора шапки регулярками."""
    return "\n".join(" ".join(cell for cell in cells if cell) for cells in rows)


def _table_rows(rows: list[list[str]]) -> list[list[str]]:
    """Строки таблицы операций на одной странице."""
    start = None
    for index, cells in enumerate(rows):
        if cells[COL_DATE].startswith(TABLE_HEADER):
            start = index + TABLE_HEADER_LINES
    if start is None:
        return []

    table = []
    for cells in rows[start:]:
        if any(cell.startswith(STOP_MARKERS) for cell in cells):
            break
        table.append(cells)
    return table


def _operations(rows: list[list[str]]) -> list[_Operation]:
    """Собрать операции из строк таблицы.

    Новая операция начинается там, где во второй колонке стоит время: в строке
    продолжения на этом месте код авторизации, а он из шести цифр.
    """
    groups: list[list[list[str]]] = []
    for cells in rows:
        if TIME_RE.match(cells[COL_TIME]):
            groups.append([])
        if groups:
            groups[-1].append(cells)
    return [_operation(group) for group in groups]


def _operation(group: list[list[str]]) -> _Operation:
    first = group[0]
    processed, auth, details, fee, original = None, "", [], Decimal(0), ""

    for cells in group[1:]:
        if DATE_RE.match(cells[COL_DATE]):
            processed = _date(cells[COL_DATE])
            auth = cells[COL_TIME]
            details.append(cells[COL_TEXT])
            # Сумма в валюте операции стоит во второй строке и появляется
            # только у покупок в чужой валюте.
            if cells[COL_AMOUNT]:
                original = cells[COL_AMOUNT]
        elif cells[COL_TEXT]:
            found = FEE_RE.match(cells[COL_TEXT])
            if found:
                # В примечании сумма без знака. Комиссия всегда расход, поэтому
                # её нога положительна независимо от знака самой операции.
                fee = abs(_money(found.group(1)))
            else:
                # Не комиссия — значит хвост описания. Молча терять его нельзя.
                details.append(cells[COL_TEXT])

    text, card = _split_tail(" ".join(part for part in details if part))
    return _Operation(
        date=_date(first[COL_DATE]),
        time=first[COL_TIME],
        processed=processed,
        auth=auth,
        category=first[COL_TEXT],
        details=text,
        amount=_money(first[COL_AMOUNT]),
        fee=fee,
        card=card,
        original=original,
    )


def _split_tail(details: str) -> tuple[str, str]:
    """Отделить от описания хвост «Операция по карте ****0001».

    Хвост есть у каждой строки и для категоризации бесполезен, а вот номер
    карты из него пригодится в метаданных. «Операция по счету» отбрасывается:
    там номер того же счёта, по которому и сделана выписка.
    """
    found = TAIL_RE.search(details)
    if found is None:
        return details.strip(), ""
    card = found.group(2) if found.group(1) == "карте" else ""
    return details[: found.start()].strip(), card


def _check_totals(operations: list[_Operation], header: str, filepath: str) -> None:
    """Сверить разобранное с оборотами, которые банк посчитал сам.

    Проверка ловит именно то, чем опасен разбор по координатам: съехавшую
    колонку и потерянные при переносе строк операции.
    """
    totals = {}
    for kind, pattern in TOTAL_RES.items():
        found = pattern.search(header)
        if found is None:
            raise ValueError(
                f"{filepath}: в шапке не найден блок «ИТОГО ПО ОПЕРАЦИЯМ ЗА ПЕРИОД» "
                f"({kind}) — разбор не с чем сверить"
            )
        # В блоке итогов суммы даны по модулю: знак несёт подпись «Пополнение»
        # или «Списание», а не само число.
        totals[kind] = abs(_money(found.group(1)))

    counted = {
        "credit": sum((op.amount for op in operations if op.amount > 0), Decimal(0)),
        "debit": -sum((op.amount for op in operations if op.amount < 0), Decimal(0)),
    }
    for kind, label in (("credit", "Пополнение"), ("debit", "Списание")):
        if counted[kind] != totals[kind]:
            raise ValueError(
                f"{filepath}: разобрано операций на {counted[kind]}, а в справке "
                f"«{label}: {totals[kind]}» — разница {counted[kind] - totals[kind]}. "
                f"Похоже, разбор потерял строки или съехал по колонкам"
            )


# ──────────────────────────────── мелочи ────────────────────────────────


def _first_page_text(filepath: str) -> str | None:
    """Текст первой страницы; None, если файл не открылся или не разобрался.

    Ловится всё подряд намеренно: pdfminer бросает с десяток разных исключений
    на битых файлах, а `identify()` обязан вернуть False, а не уронить импорт.
    """
    try:
        with pdfplumber.open(filepath) as pdf:
            if not pdf.pages:
                return None
            return _text(_rows(pdf.pages[0]))
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return None


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _date(text: str) -> dt.date:
    return dt.datetime.strptime(text.strip(), "%d.%m.%Y").date()


def _money(text: str) -> Decimal:
    """Разобрать `+2 050 679,78` или `3 844,00`.

    Разделитель тысяч — неразрывный пробел, десятичный — запятая. Знак стоит
    только у прихода: расход банк печатает без минуса, и это надо помнить —
    сумма без знака отрицательная.
    """
    found = MONEY_RE.match(text.strip())
    if found is None:
        raise ValueError(f"не разобрать сумму {text!r}")
    digits = found.group(2).replace("\u00a0", "").replace(" ", "")
    value = Decimal(f"{digits}.{found.group(3)}")
    return value if found.group(1) == "+" else -value


def _key(number: str, operation: _Operation, seen: dict[str, int]) -> str:
    """Ключ дедупа: номер счёта, дата и код авторизации.

    Код авторизации уникален сам по себе, но дата в ключе страхует от его
    повторного использования банком в другом периоде. Порядковый номер в
    хвосте — на случай, если код всё-таки повторится в один день: без него
    вторая такая операция молча считалась бы дубликатом первой.
    """
    base = f"{number}:{operation.date:%Y-%m-%d}:{operation.auth}"
    seen[base] = seen.get(base, 0) + 1
    return f"{base}-{seen[base]}"
