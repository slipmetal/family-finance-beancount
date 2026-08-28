"""Импортёры выписок Ameriabank (Армения).

Банк отдаёт CSV двух разных форматов, и общего у них только кодировка. Поэтому
здесь два импортёра, как у ACBA:

* `CardImporter` — **выписка по карте**. Одна знаковая сумма с кодом валюты
  внутри поля, время операции, но контрагент только именем:

      "Transaction date","Settlement date","Transaction type","Receiver/Sender",
      "Transaction details","Transaction amount","Transaction amount in account currency"
      "19/01/26, 14:12","19/01/26, 14:12","Արտարժույթի փոխանակում",…,"+2,500.00 AMD"

* `AccountImporter` — **выписка по счёту** (в том числе сберегательному).
  Раздельные Credit/Debit без знака, даты без времени, зато есть номер счёта
  контрагента и строка оборотов в конце:

      Date,Document No,Operation Type,Account,Recipient/Sender,Purpose,Category,Credit,Debit
      26.03.2026,181987,Between my accounts,1000012345678901,…,Transfer of own funds,,0,700.0

Особенности карточного формата, из-за которых нельзя обойтись голым `str.split`:
  * внутри поля даты есть запятая, поэтому нужен настоящий CSV-парсер;
  * сумма приходит одной знаковой строкой с разделителем тысяч и кодом валюты
    внутри поля (`-2,500.00 AMD`), отдельных колонок дебет/кредит нет;
  * колонка `Transaction type` почти бесполезна для категоризации — 454 из 564
    строк в образце это `Քարտային գործարք`, куда свалены и покупки, и возвраты,
    и банкомат, и комиссии. Категории подбираются по `Transaction details`
    правилами из rules.yaml, а тип уходит в метаданные.

Общее у обоих: **номера счёта в файле нет**, поэтому счёт в общем случае
определяется меткой в имени файла. Метка, а не папка: fava кладёт всё
загруженное через браузер в одну папку, и выписка второго счёта молча
досталась бы первому.

Метка нужна не всегда. Кое-что о счёте в файле всё-таки есть — код валюты
в карточной выписке и хвост номера в описании процентов в выписке по счёту, —
и если этого хватает, чтобы отличить счёт от остальных счетов Ameriabank
в accounts.yaml, импортёр опознаёт свой файл как угодно названным. Считает это
finance/config.py и передаёт сюда флагом `marker_optional`: одному импортёру
такое не решить, ему не видны соседи.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import beangulp
from beancount.core import amount as bc_amount
from beancount.core import data, flags
from beangulp import extract
from beangulp.importers import csvbase

from finance.booking import categorize
from finance.categorize import Rules

#: Ожидаемая шапка файла. Служит подписью формата в `identify()`.
COLUMNS = (
    "Transaction date",
    "Settlement date",
    "Transaction type",
    "Receiver/Sender",
    "Transaction details",
    "Transaction amount",
    "Transaction amount in account currency",
)

#: Формат обеих дат: `19/01/26, 14:12`. Год двузначный.
DATE_FORMAT = "%d/%m/%y, %H:%M"

#: Время внутри поля даты — beancount хранит только дату, время идёт в метаданные.
TIME_RE = re.compile(r",\s*(\d{2}:\d{2})\s*$")

#: Код валюты в хвосте суммы: `-2,500.00 AMD`.
CURRENCY_RE = re.compile(r"([A-Z]{3})\s*$")

#: Префикс, которым банк помечает карточные операции в описании.
CARD_PREFIX = "Ք: "


class Time(csvbase.Column):
    """Время из поля вида `19/01/26, 14:12`."""

    def parse(self, value):
        found = TIME_RE.search(value)
        return found.group(1) if found else ""


class CardImporter(csvbase.Importer):
    """Ameriabank (Armenia) card CSV statement."""

    # Без utf-8-sig BOM приклеивается к имени первой колонки и ломает поиск по шапке.
    encoding = "utf-8-sig"
    names = True
    # Выписка идёт по возрастанию дат. Задаём явно: иначе csvbase выводит порядок
    # из первого файла и кеширует его в инстансе импортёра на все последующие.
    order = csvbase.Order.ASCENDING

    date = csvbase.Date(COLUMNS[0], DATE_FORMAT)
    time = Time(COLUMNS[0])
    settlement = csvbase.Date(COLUMNS[1], DATE_FORMAT)
    txn_type = csvbase.Column(COLUMNS[2])
    counterparty = csvbase.Column(COLUMNS[3])
    narration = csvbase.Column(COLUMNS[4])
    # Разделитель тысяч и код валюты убираем до разбора в Decimal.
    amount = csvbase.Amount(COLUMNS[6], subs={r",": "", r"\s*[A-Z]{3}\s*$": ""})
    # Та же колонка сырой строкой — только чтобы сверить код валюты.
    raw_amount = csvbase.Column(COLUMNS[6])
    # Сумма в валюте операции. Обычно совпадает с суммой в валюте счёта, но у
    # покупок за рубежом отличается: `-3.49 USD` против `-1,329.69 AMD`.
    raw_original = csvbase.Column(COLUMNS[5])

    def __init__(
        self,
        account: str,
        currency: str,
        rules: Rules,
        *,
        marker: str,
        marker_optional: bool = False,
        flag: str = flags.FLAG_OKAY,
    ):
        super().__init__(account, currency, flag)
        self.rules = rules
        self.marker = marker.lower()
        # Считает finance/config.py: ему видны все счета сразу, а по одному
        # счёту узнать, единственный ли он в своей валюте, нельзя.
        self.marker_optional = marker_optional

    @property
    def name(self) -> str:
        """Имя обязано быть уникальным: fava отвергает конфиг с повторами,
        а экземпляров этого класса три — по одному на счёт."""
        return f"ameria.{self.marker}"

    def identify(self, filepath: str) -> bool:
        """Опознать файл по шапке, метке в имени и валюте.

        В выписке Ameriabank нет ни номера счёта, ни номера карты, и валюта —
        единственный признак счёта, который лежит в самом файле. Поэтому две
        карты в одной валюте различимы только по тому, как назван файл: имя
        должно содержать метку счёта (`card6718`, `card7080`).

        Раньше вместо метки использовалась папка, но это несовместимо с
        загрузкой через браузер: fava кладёт любой загруженный файл в одну и ту
        же папку, и выписка второй карты молча досталась бы первой.

        А карта, чья валюта в accounts.yaml единственная, опознаёт себя сама,
        и переименовывать её выписку незачем — про это и говорит
        `marker_optional`. Требование к содержимому тогда строже: с меткой
        валюта имеет право вето (у пустой выписки её не узнать, и мы верим
        имени файла), без метки — обязана подтвердиться. Иначе пустая выписка
        досталась бы первому же счёту, у которого метка необязательна.
        """
        path = Path(filepath)
        if path.suffix.lower() != ".csv":
            return False

        named = self.marker in path.name.lower()
        # Файл, названный не нами, и читать незачем — он либо чужой, либо
        # безымянный, и во втором случае нас спасёт только уникальная валюта.
        if not named and not self.marker_optional:
            return False

        header, currency = self._peek(path)
        if header != COLUMNS:
            return False
        if named:
            return currency is None or currency == self.currency
        return currency == self.currency

    def _peek(self, path: Path) -> tuple[tuple[str, ...] | None, str | None]:
        """Прочитать шапку и валюту первой строки, не разбирая файл целиком."""
        try:
            with open(path, encoding=self.encoding) as fd:
                reader = csv.reader(fd)
                header = next(reader, None)
                first = next(reader, None)
        except (OSError, UnicodeDecodeError, csv.Error):
            return None, None
        if header is None:
            return None, None

        currency = None
        if first is not None and len(first) == len(COLUMNS):
            found = CURRENCY_RE.search(first[6])
            currency = found.group(1) if found else None
        return tuple(name.strip() for name in header), currency

    def filename(self, filepath: str) -> str:
        """Человекочитаемое имя для архива: дату и счёт beangulp подставит сам."""
        return "ameria.csv"

    def metadata(self, filepath: str, lineno: int, row) -> dict:
        meta = super().metadata(filepath, lineno, row)
        if row.time:
            meta["time"] = row.time
        meta["bank-type"] = row.txn_type
        # Дата списания указывается почти всегда, но отличается от даты операции
        # только у карточных транзакций — храним лишь когда она что-то добавляет.
        if row.settlement != row.date:
            meta["settlement"] = row.settlement
        # Покупка за рубежом: сколько было списано в валюте продавца.
        if row.raw_original != row.raw_amount:
            meta["original"] = row.raw_original
        return meta

    def finalize(self, txn: data.Transaction, row) -> data.Transaction:
        self._check_currency(row, txn.meta.get("lineno"))
        return categorize(
            txn,
            self.rules,
            counterparty=row.counterparty,
            details=row.narration,
            txn_type=row.txn_type,
            amount=row.amount,
            # Банк обрезает описание до 34 символов, но другого источника нет.
            narration=_clean_narration(row.narration),
            ok_flag=self.flag,
        )

    def _check_currency(self, row, lineno) -> None:
        """Убедиться, что валюта строки совпадает с валютой счёта.

        Выписка образца целиком в AMD, но мультивалютный счёт должен упасть
        с внятной ошибкой, а не молча записать чужие суммы как драмы.
        """
        found = CURRENCY_RE.search(row.raw_amount)
        if found is None:
            raise ValueError(f"строка {lineno}: не разобрать код валюты в {row.raw_amount!r}")
        if found.group(1) != self.currency:
            raise ValueError(
                f"строка {lineno}: валюта {found.group(1)} не совпадает с валютой "
                f"счёта {self.currency}; нужен отдельный импортёр под этот счёт"
            )


def _clean_narration(details: str) -> str:
    """Убрать служебный префикс карточных операций и лишние пробелы."""
    text = details.strip()
    if text.startswith(CARD_PREFIX):
        text = text[len(CARD_PREFIX) :]
    return re.sub(r"\s{2,}", " ", text).strip()


# ─────────────────────── выписка по счёту: другой CSV ───────────────────────

#: Шапка выписки по счёту. Служит подписью формата в `identify()`.
ACCOUNT_COLUMNS = (
    "Date",
    "Document No",
    "Operation Type",
    "Account",
    "Recipient/Sender",
    "Purpose",
    "Category",
    "Credit",
    "Debit",
)
(
    COL_DATE,
    COL_DOCUMENT,
    COL_TYPE,
    COL_CORRESPONDENT,
    COL_COUNTERPARTY,
    COL_PURPOSE,
    COL_CATEGORY,
    COL_CREDIT,
    COL_DEBIT,
) = range(len(ACCOUNT_COLUMNS))

ACCOUNT_DATE_FORMAT = "%d.%m.%Y"

#: Хвост номера счёта внутри описания процентов и налога с них:
#: «% կապիտ. ըստ 53294282901 հաշվի». Единственное место во всём файле, где
#: номер счёта вообще встречается, — и то не в каждой выписке.
ACCOUNT_TAIL_RE = re.compile(r"ըստ\s+(\d{6,})")


class AccountImporter(beangulp.Importer):
    """Ameriabank (Armenia) account CSV statement."""

    encoding = "utf-8-sig"

    def __init__(
        self,
        account: str,
        currency: str,
        rules: Rules,
        *,
        marker: str,
        number: str = "",
        marker_optional: bool = False,
    ):
        self.importer_account = account
        self.currency = currency
        self.rules = rules
        self.marker = marker.lower()
        self.number = number
        # Считает finance/config.py — см. CardImporter.
        self.marker_optional = marker_optional

    @property
    def name(self) -> str:
        """Префикс отличается от карточного: метки задаёт человек, и ничто не
        мешает ему совпасть у выписки по карте и по счёту."""
        return f"ameria-account.{self.marker}"

    def identify(self, filepath: str) -> bool:
        """Опознать файл по шапке, метке в имени и хвосту номера счёта.

        Номера счёта в файле нет — как и в карточной выписке, — а валюты в этом
        формате нет вовсе: ни колонки, ни кода в сумме. Единственный признак
        счёта внутри файла — хвост номера, который банк печатает в описании
        начисленных процентов, и тот появляется не в каждой выписке.

        С меткой в имени хвост работает правом вето: нашёлся и не сошёлся —
        файл не опознаётся, вместо того чтобы уехать не на тот счёт. Не нашёлся
        — верим метке, иначе выписка за месяц без процентов не импортировалась
        бы вовсе.

        Без метки наоборот: хвост обязан найтись и сойтись. Тогда файл можно не
        переименовывать — но только если по последним цифрам номера этот счёт
        отличается от остальных счетов Ameriabank, о чём и говорит
        `marker_optional` (считает finance/config.py).
        """
        path = Path(filepath)
        if path.suffix.lower() != ".csv":
            return False

        named = self.marker in path.name.lower()
        if not named and not self.marker_optional:
            return False

        rows = _read(path, self.encoding)
        if rows is None or tuple(name.strip() for name in rows[0]) != ACCOUNT_COLUMNS:
            return False

        tail = _account_tail(rows[1:])
        if named:
            return tail is None or not self.number or self.number.endswith(tail)
        return bool(self.number) and tail is not None and self.number.endswith(tail)

    def account(self, filepath: str) -> str:
        return self.importer_account

    def date(self, filepath: str) -> dt.date | None:
        """Дат периода в файле нет — берём последнюю операцию."""
        rows = _read(Path(filepath), self.encoding)
        dates = [_date(r[COL_DATE]) for r in (rows or [])[1:] if r and r[COL_DATE].strip()]
        return max(dates) if dates else None

    def filename(self, filepath: str) -> str:
        return f"ameria-{self.currency.lower()}-account.csv"

    def extract(self, filepath: str, existing: data.Entries) -> data.Entries:
        rows = _read(Path(filepath), self.encoding)
        if rows is None:
            return []

        body = [r for r in rows[1:] if r and r[COL_DATE].strip()]
        totals = [r for r in rows[1:] if r and not r[COL_DATE].strip()]
        _check_totals(body, totals, filepath)

        seen: dict[str, int] = {}
        return [self._transaction(filepath, i, row, seen) for i, row in enumerate(body)]

    def _transaction(self, filepath: str, index: int, row, seen: dict[str, int]) -> data.Transaction:
        value = _number(row[COL_CREDIT]) - _number(row[COL_DEBIT])

        meta = data.new_metadata(filepath, index + 2)
        meta["ameria-id"] = _account_key(self.marker, row, seen)
        if row[COL_TYPE].strip():
            meta["bank-type"] = row[COL_TYPE].strip()
        if row[COL_CORRESPONDENT].strip():
            meta["correspondent"] = row[COL_CORRESPONDENT].strip()

        txn = data.Transaction(
            meta,
            _date(row[COL_DATE]),
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
            counterparty=row[COL_COUNTERPARTY].strip(),
            details=row[COL_PURPOSE].strip(),
            amount=value,
            # Тип операции здесь, в отличие от карточной выписки, осмысленный:
            # `Interest repayment`, `Tax charge`, `Currency exchange` — по нему
            # и пишутся правила, причём по-английски, а не по-армянски.
            txn_type=row[COL_TYPE].strip(),
            # Номер счёта контрагента есть только в этом формате. По нему
            # опознаются переводы на свои же счета — по номеру, а не по имени.
            correspondent=row[COL_CORRESPONDENT].strip(),
        )

    def deduplicate(self, entries: data.Entries, existing: data.Entries) -> None:
        """Точный дедуп по номеру документа вместо эвристики по дате и сумме."""
        known = {
            entry.meta["ameria-id"]: entry
            for entry in existing
            if isinstance(entry, data.Transaction) and "ameria-id" in entry.meta
        }
        for entry in entries:
            match = known.get(entry.meta.get("ameria-id"))
            if match is not None:
                entry.meta[extract.DUPLICATE] = match


def _read(path: Path, encoding: str) -> list[list[str]] | None:
    try:
        with open(path, encoding=encoding, newline="") as fd:
            rows = list(csv.reader(fd))
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    return rows if rows else None


def _account_tail(rows) -> str | None:
    """Последние цифры номера счёта из описания процентов, если они там есть."""
    for row in rows:
        if len(row) > COL_PURPOSE:
            found = ACCOUNT_TAIL_RE.search(row[COL_PURPOSE])
            if found:
                return found.group(1)
    return None


def _number(text: str) -> Decimal:
    """Разобрать сумму из колонки Credit или Debit. Пустая клетка — ноль."""
    cleaned = (text or "").strip().replace(",", "")
    if not cleaned:
        return Decimal(0)
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"не разобрать сумму {text!r}") from exc


def _date(text: str) -> dt.date:
    return dt.datetime.strptime(text.strip(), ACCOUNT_DATE_FORMAT).date()


def _account_key(marker: str, row, seen: dict[str, int]) -> str:
    """Ключ дедупа: метка счёта, дата и номер документа.

    Номер документа сам по себе не годится: у процентов и удержанного с них
    налога он общий, а у части операций его нет вовсе. Дата в ключе тоже
    обязательна — номера короткие и сквозные внутри периода (`172`, `198`,
    `203`), так что в следующем году они пойдут по второму кругу.
    """
    base = f"{marker}:{_date(row[COL_DATE]):%Y-%m-%d}:{row[COL_DOCUMENT].strip()}"
    seen[base] = seen.get(base, 0) + 1
    return f"{base}-{seen[base]}"


def _check_totals(body, totals, filepath: str) -> None:
    """Сверить разобранное с оборотами из последней строки выписки.

    Строка оборотов у Ameriabank идёт последней и отличается пустой датой.
    Проверка ловит потерянные при разборе строки — то же, что делают сверки
    у Т-Банка и Сбербанка.
    """
    if not totals:
        raise ValueError(f"{filepath}: в выписке нет строки оборотов — разбор не с чем сверить")

    row = totals[-1]
    for column, label in ((COL_CREDIT, "приход"), (COL_DEBIT, "расход")):
        counted = sum((_number(r[column]) for r in body), Decimal(0))
        stated = _number(row[column])
        if counted != stated:
            raise ValueError(
                f"{filepath}: разобрано операций на {counted} ({label}), а в выписке "
                f"{stated} — разница {counted - stated}. Похоже, разбор потерял строки"
            )
