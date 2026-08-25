"""Импортёр «Справки о движении средств» Т-Банка (Россия).

Формат — PDF, и выбора тут нет: ни CSV, ни выгрузки по API банк не даёт.
Справка собрана JasperReports, текстовый слой в ней полноценный, так что
распознавание не нужно — достаточно прочитать слова вместе с координатами.

Таблица операций размечена **позициями, а не разделителями**: у каждой колонки
свой левый край, и он одинаков на всех страницах. Отсюда весь разбор:

  * ячейка определяется диапазоном между соседними началами колонок. Точное
    совпадение x не годится: сумму и знак валюты банк рисует одним куском, и x
    знака зависит от ширины суммы;
  * одна операция занимает несколько строк — время во второй, описание
    переносится (в реальных справках попадалось до семи строк);
  * шапка таблицы повторяется на каждой странице, внизу каждой — реквизиты
    банка. И то и другое приходится узнавать и пропускать.

Три особенности формата, на которых легко ошибиться и которые здесь учтены:

  * **своего номера операции в справке нет вообще.** Эвристический дедуп
    beangulp здесь опасен: в реальной выписке десятки одинаковых списаний
    «Перевод для пополнения счета Инвесткопилка» по 5.50 ₽ в соседние дни, и
    окно ±2 дня с допуском 5 % склеило бы их в одну. Поэтому ключ собирается
    из содержимого строки — см. `_key`;
  * **в блоке итогов десятичный разделитель — запятая**, хотя в самой таблице
    точка. Единственное место во всей справке, и разбор проверяет это явно;
  * **сумма операции и сумма в валюте счёта — разные колонки.** Покупка в
    Армении рублёвой картой приходит как `-2 000.00 Դ` и `-463.24 ₽`; в леджер
    идёт вторая, первая уезжает в метаданные.
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

#: Левые края колонок таблицы операций, в пунктах от края страницы.
COLUMNS = (56, 126, 199, 294, 389, 499)
COL_DATE, COL_SETTLEMENT, COL_ORIGINAL, COL_AMOUNT, COL_DETAILS, COL_CARD = range(len(COLUMNS))

#: Допуск при отнесении слова к колонке: банк ставит их ровно, но округление
#: координат в PDF даёт доли пункта.
COLUMN_TOLERANCE = 1.0

#: На сколько пунктов ячейки одной строки могут разъезжаться по вертикали.
#: Знак драма приходит из другого шрифта и садится примерно на пункт выше
#: соседей по строке, а соседние строки таблицы отстоят на 11 — порог с запасом.
ROW_TOLERANCE = 3.0

#: Заголовок документа. Служит подписью формата в `identify()`: он пережил
#: переименование Тинькофф → Т-Банк, в отличие от названия банка в шапке.
TITLE = "Справка о движении средств"

#: Первая ячейка шапки таблицы. Шапка занимает две строки.
TABLE_HEADER = "Дата и время"

#: Реквизиты банка в подвале каждой страницы — на них таблица заканчивается.
PAGE_FOOTER = "АО «ТБанк»"

#: Блок итогов. В длинных справках он на отдельной последней странице, в
#: коротких идёт сразу за таблицей — поэтому ищется на всех страницах и заодно
#: служит признаком конца таблицы.
TOTAL_LABELS = {"Пополнения:": "credit", "Расходы:": "debit"}

ACCOUNT_NUMBER_RE = re.compile(r"Номер лицевого счета:?\s*(\d{6,})")
PERIOD_RE = re.compile(r"за период с\s*(\d{2}\.\d{2}\.\d{4})\s*по\s*(\d{2}\.\d{2}\.\d{4})")
BALANCE_RE = re.compile(r"Сумма доступного остатка на\s*(\d{2}\.\d{2}\.\d{4})\s*:\s*(.+)")

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
CARD_RE = re.compile(r"^\d{4}$")

#: Сумма вместе со знаком валюты: `-2 000.00 Դ`, `+33 000.00 ₽`, `1 668 291,06 ₽`.
#: Разделитель тысяч — обычный пробел.
MONEY_RE = re.compile(r"^([-+]?)\s*([\d ]+)([.,])(\d{2})\s*(\S+)$")

#: Валюту банк обозначает знаком, а не кодом. Добавлять новые нужно по факту
#: встречи в выписке: догадка тут молча увела бы сумму не в ту валюту.
CURRENCY_SIGNS = {"₽": "RUB", "Դ": "AMD"}


class Importer(beangulp.Importer):
    """T-Bank (Russia) account statement (.pdf)."""

    def __init__(self, account: str, currency: str, number: str, rules: Rules):
        self.importer_account = account
        self.currency = currency
        self.number = number
        self.rules = rules

    @property
    def name(self) -> str:
        """Имя обязано быть уникальным: fava отвергает конфиг с повторами."""
        return f"tbank.{self.importer_account.rsplit(':', 1)[-1].lower()}"

    def identify(self, filepath: str) -> bool:
        """Опознать справку по заголовку и номеру лицевого счёта внутри файла.

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
        return found is not None and found.group(1) == self.number

    def account(self, filepath: str) -> str:
        return self.importer_account

    def date(self, filepath: str) -> dt.date | None:
        """Конец периода выписки. None — beangulp возьмёт дату файла."""
        text = _first_page_text(filepath)
        found = PERIOD_RE.search(text) if text is not None else None
        return _date(found.group(2)) if found else None

    def filename(self, filepath: str) -> str:
        return f"tbank-{self.currency.lower()}.pdf"

    def extract(self, filepath: str, existing: data.Entries) -> data.Entries:
        with pdfplumber.open(filepath) as pdf:
            pages = [_rows(page) for page in pdf.pages]
        if not pages:
            return []

        operations: list[_Operation] = []
        totals: dict[str, Decimal] = {}
        for rows in pages:
            operations += [_operation(group) for group in _groups(_table_rows(rows))]
            totals.update(_totals(rows))

        # Порядок задаётся явно: банк отдаёт справку от новых операций к старым,
        # и полагаться на это, чтобы просто развернуть список, не хочется.
        # Сортировка устойчивая, так что одинаковые метки времени сохраняют
        # взаимный порядок — от него зависит нумерация в ключе дедупа.
        operations.sort(key=lambda op: (op.date, op.time))
        _check_totals(operations, totals, filepath)

        seen: dict[str, int] = {}
        entries: data.Entries = [
            self._transaction(filepath, index, operation, seen)
            for index, operation in enumerate(operations)
        ]

        balance = self._balance(filepath, pages[0])
        if balance is not None:
            entries.append(balance)
        return entries

    def _transaction(
        self, filepath: str, index: int, operation: _Operation, seen: dict[str, int]
    ) -> data.Transaction:
        meta = data.new_metadata(filepath, index + 1)
        meta["tbank-id"] = _key(self.number, operation, seen)
        if operation.time:
            meta["time"] = operation.time
        # Дата списания отличается от даты операции только у части карточных
        # операций — храним лишь когда она что-то добавляет.
        if operation.settlement is not None and operation.settlement != operation.date:
            meta["settlement"] = operation.settlement.strftime("%Y-%m-%d")
        # Покупка в чужой валюте: сколько было списано у продавца.
        if operation.original_currency and operation.original_currency != self.currency:
            meta["original"] = f"{operation.original} {operation.original_currency}"
        if operation.card:
            meta["card"] = operation.card

        if operation.currency != self.currency:
            raise ValueError(
                f"{filepath}, операция {index + 1}: валюта {operation.currency} не "
                f"совпадает с валютой счёта {self.currency}; нужен отдельный "
                f"импортёр под этот счёт"
            )

        txn = data.Transaction(
            meta,
            operation.date,
            flags.FLAG_OKAY,
            None,
            "",
            frozenset(),
            frozenset(),
            [
                data.Posting(
                    self.importer_account,
                    bc_amount.Amount(operation.amount, self.currency),
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
            # Отдельных колонок контрагента и типа операции в справке нет: и то
            # и другое лежит внутри описания («Оплата в OZON Moskva RUS»,
            # «Внутренний перевод на договор 8204157546»). Правила пишутся
            # по `details`, и это описано в rules.example.yaml.
            counterparty="",
            details=operation.details,
            txn_type="",
            amount=operation.amount,
        )

    def _balance(self, filepath: str, rows: list[list[str]]) -> data.Balance | None:
        """Сверка с остатком, указанным в шапке справки."""
        found = BALANCE_RE.search(_text(rows))
        if found is None:
            return None
        value, currency = _money(found.group(2), decimal=".")
        if currency != self.currency:
            return None
        # Balance проверяет остаток на начало дня — берём следующий.
        return data.Balance(
            data.new_metadata(filepath, 0),
            _date(found.group(1)) + dt.timedelta(days=1),
            self.importer_account,
            bc_amount.Amount(value, self.currency),
            None,
            None,
        )

    def deduplicate(self, entries: data.Entries, existing: data.Entries) -> None:
        """Точный дедуп по ключу операции вместо эвристики по дате и сумме.

        Эвристика здесь опасна: своего номера операции банк не даёт, а
        одинаковых мелких списаний в справке сотни — окно ±2 дня с допуском 5 %
        схлопнуло бы разные операции в одну.
        """
        known = {
            entry.meta["tbank-id"]: entry
            for entry in existing
            if isinstance(entry, data.Transaction) and "tbank-id" in entry.meta
        }
        for entry in entries:
            match = known.get(entry.meta.get("tbank-id"))
            if match is not None:
                entry.meta[extract.DUPLICATE] = match


# ──────────────────────────── разбор страницы ────────────────────────────


@dataclass(frozen=True)
class _Operation:
    """Одна операция, собранная из нескольких строк таблицы."""

    date: dt.date
    time: str
    settlement: dt.date | None
    #: Сумма в валюте счёта — она и идёт в проводку.
    amount: Decimal
    currency: str
    #: Сумма в валюте операции: у покупки за рубежом отличается от суммы счёта.
    original: Decimal
    original_currency: str
    details: str
    card: str


def _column(x: float) -> int | None:
    """Номер колонки, которой принадлежит слово, начинающееся на x."""
    for index in reversed(range(len(COLUMNS))):
        if x >= COLUMNS[index] - COLUMN_TOLERANCE:
            return index
    return None


def _rows(page) -> list[list[str]]:
    """Строки страницы, разложенные по колонкам таблицы.

    `keep_blank_chars` обязателен: без него сумма `-2 000.00` разрезается по
    разделителю тысяч на два слова.
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
    """Строки таблицы операций на одной странице.

    Страница без шапки таблицы операций не содержит — так устроена последняя
    страница с итогами и подписью.
    """
    start = None
    for index, cells in enumerate(rows):
        if cells[COL_DATE].startswith(TABLE_HEADER):
            # Шапка занимает две строки: «Дата и время» / «операции».
            start = index + 2
    if start is None:
        return []

    table = []
    for cells in rows[start:]:
        if any(cell.startswith(PAGE_FOOTER) for cell in cells):
            break
        if cells[COL_DATE] in TOTAL_LABELS:
            break
        table.append(cells)
    return table


def _groups(rows: list[list[str]]) -> list[list[list[str]]]:
    """Разбить строки таблицы на операции: новая начинается со строки с датой."""
    groups: list[list[list[str]]] = []
    for cells in rows:
        if DATE_RE.match(cells[COL_DATE]):
            groups.append([])
        if groups:
            groups[-1].append(cells)
    return groups


def _operation(group: list[list[str]]) -> _Operation:
    first = group[0]
    amount, currency = _money(first[COL_AMOUNT], decimal=".")
    original, original_currency = (
        _money(first[COL_ORIGINAL], decimal=".") if first[COL_ORIGINAL] else (amount, currency)
    )
    return _Operation(
        date=_date(first[COL_DATE]),
        time=_time(group),
        settlement=_date(first[COL_SETTLEMENT]) if first[COL_SETTLEMENT] else None,
        amount=amount,
        currency=currency,
        original=original,
        original_currency=original_currency,
        # Описание переносится по словам, поэтому строки склеиваются пробелом.
        # Длинные неразрывные куски (идентификаторы договоров) банк рвёт прямо
        # посередине, и там пробел лишний, — но отличить один случай от другого
        # по готовому PDF нельзя, а страдают только служебные номера.
        details=_clean(" ".join(cells[COL_DETAILS] for cells in group if cells[COL_DETAILS])),
        card=first[COL_CARD] if CARD_RE.match(first[COL_CARD]) else "",
    )


def _time(group: list[list[str]]) -> str:
    """Время операции: банк ставит его второй строкой под датой."""
    for cells in group[1:]:
        if TIME_RE.match(cells[COL_DATE]):
            return cells[COL_DATE]
    return ""


def _totals(rows: list[list[str]]) -> dict[str, Decimal]:
    """Обороты за период из блока итогов: подпись слева, сумма в соседней колонке."""
    found = {}
    for cells in rows:
        kind = TOTAL_LABELS.get(cells[COL_DATE])
        if kind is not None:
            found[kind] = _money(cells[COL_SETTLEMENT], decimal=",")[0]
    return found


def _check_totals(
    operations: list[_Operation], totals: dict[str, Decimal], filepath: str
) -> None:
    """Сверить разобранное с оборотами, которые банк посчитал сам.

    Проверка ловит именно то, чем опасен разбор по координатам: съехавшую
    колонку и потерянные при переносе строки. Обе суммы даются в сообщении,
    чтобы было видно, на сколько именно разошлось.
    """
    missing = sorted(set(TOTAL_LABELS.values()) - set(totals))
    if missing:
        raise ValueError(
            f"{filepath}: в справке не найден блок итогов "
            f"({', '.join(sorted(TOTAL_LABELS))}) — разбор не с чем сверить"
        )

    counted = {
        "credit": sum((op.amount for op in operations if op.amount > 0), Decimal(0)),
        "debit": -sum((op.amount for op in operations if op.amount < 0), Decimal(0)),
    }
    for kind, label in (("credit", "Пополнения"), ("debit", "Расходы")):
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
    except Exception:  # noqa: BLE001 — см. докстринг
        return None


def _date(text: str) -> dt.date:
    return dt.datetime.strptime(text.strip(), "%d.%m.%Y").date()


def _money(text: str, *, decimal: str) -> tuple[Decimal, str]:
    """Разобрать `-2 000.00 Դ` в сумму и код валюты.

    `decimal` задаётся явно, потому что в таблице операций разделитель точка,
    а в блоке итогов — запятая. Без проверки смена формата проехала бы молча.
    """
    found = MONEY_RE.match(text.strip())
    if found is None:
        raise ValueError(f"не разобрать сумму {text!r}")
    if found.group(3) != decimal:
        raise ValueError(
            f"в сумме {text!r} десятичный разделитель {found.group(3)!r}, "
            f"а ожидался {decimal!r}"
        )
    sign = found.group(5)
    if sign not in CURRENCY_SIGNS:
        raise ValueError(
            f"неизвестный знак валюты {sign!r} в {text!r}; "
            f"известны {', '.join(CURRENCY_SIGNS)}"
        )
    digits = found.group(2).replace(" ", "")
    return Decimal(f"{found.group(1)}{digits}.{found.group(4)}"), CURRENCY_SIGNS[sign]


def _key(number: str, operation: _Operation, seen: dict[str, int]) -> str:
    """Ключ дедупа: банк своего номера операции не даёт, собираем из содержимого.

    Дата списания в ключ намеренно не входит: у операции, которая на момент
    выгрузки ещё не проведена, она появится в следующей справке, и ключ бы
    сменился — та же операция приехала бы во второй раз.

    Порядковый номер в хвосте различает совпавшие до минуты и до копейки
    операции: четыре одинаковые комиссии подряд — это четыре операции, а не
    одна, и схлопывать их нельзя.
    """
    base = f"{number}:{operation.date:%Y-%m-%d}T{operation.time}:{operation.amount}"
    seen[base] = seen.get(base, 0) + 1
    return f"{base}-{seen[base]}"


def _clean(text: str) -> str:
    return re.sub(r"\s{2,}", " ", text).strip()