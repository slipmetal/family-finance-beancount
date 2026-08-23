"""Импортёр выписок Ameriabank (Армения).

Формат: CSV из интернет-банка, UTF-8 с BOM, все поля закавычены.

    "Transaction date","Settlement date","Transaction type","Receiver/Sender",
    "Transaction details","Transaction amount","Transaction amount in account currency"
    "19/01/26, 14:12","19/01/26, 14:12","Արտարժույթի փոխանակում",...,"+2,500.00 AMD","+2,500.00 AMD"

Особенности, из-за которых нельзя обойтись голым `str.split`:
  * внутри поля даты есть запятая, поэтому нужен настоящий CSV-парсер;
  * сумма приходит одной знаковой строкой с разделителем тысяч и кодом валюты
    внутри поля (`-2,500.00 AMD`), отдельных колонок дебет/кредит нет;
  * колонка `Transaction type` почти бесполезна для категоризации — 454 из 564
    строк в образце это `Քարտային գործարք`, куда свалены и покупки, и возвраты,
    и банкомат, и комиссии. Категории подбираются по `Transaction details`
    правилами из rules.yaml, а тип уходит в метаданные.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from beancount.core import data, flags
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


class Importer(csvbase.Importer):
    """Ameriabank (Armenia) CSV statement."""

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
        flag: str = flags.FLAG_OKAY,
    ):
        super().__init__(account, currency, flag)
        self.rules = rules
        self.marker = marker.lower()

    @property
    def name(self) -> str:
        """Имя обязано быть уникальным: fava отвергает конфиг с повторами,
        а экземпляров этого класса три — по одному на счёт."""
        return f"ameria.{self.marker}"

    def identify(self, filepath: str) -> bool:
        """Опознать файл по метке в имени, шапке и валюте.

        В выписке Ameriabank нет ни номера счёта, ни номера карты, поэтому два
        драмовых счёта различимы только по тому, как назван файл: имя должно
        содержать метку счёта (`card6718`, `card7080`, `rub`).

        Раньше вместо метки использовалась папка, но это несовместимо с
        загрузкой через браузер: fava кладёт любой загруженный файл в одну и ту
        же папку, и выписка второй карты молча досталась бы первой.

        Валюта — подстраховка на случай ошибки в метке. Проверять содержимое
        обязательно: beangulp падает, если один файл опознали два импортёра.
        """
        path = Path(filepath)
        if path.suffix.lower() != ".csv":
            return False
        if self.marker not in path.name.lower():
            return False

        header, currency = self._peek(path)
        if header != COLUMNS:
            return False
        # Валюта — право вето, а не требование: у пустой выписки её не узнать.
        return currency is None or currency == self.currency

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
