#!/usr/bin/env python3
"""Забрать курсы валют у Центробанка Армении и разложить их в директивы `price`.

    python tools/fetch_rates.py                       # с начала года по сегодня
    python tools/fetch_rates.py --from 2026-01-01 --to 2026-08-26
    python tools/fetch_rates.py --currencies RUB,USD,EUR,GEL
    python tools/fetch_rates.py --stdout              # посмотреть, не записывая

Зачем это нужно. Операционная валюта леджера — AMD, а доход приходит в рублях:
без курсов fava рисует рублёвый доход и драмовые расходы на одном графике как
есть, и отчёт в драмах показывает дефицит, которого нет. Директивы `price`
дают beancount то, по чему приводить одно к другому.

Источник — SOAP-сервис ЦБ Армении, метод ExchangeRatesByDateRangeByISO:
https://www.cba.am/en/exchange-rates-retrieval/

Почему конверт собирается руками, а не через zeep. Сервис отдаёт .NET-овский
DataSet, завёрнутый в diffgram: инлайновая схема объявляет элемент `NewDataSet`,
а в теле приходит `DocumentElement`, которого в схеме нет. Zeep на этом падает
с LookupError — и в строгом режиме, и в нестрогом. Обойти можно только
raw_response, а это низводит его до обычного HTTP-клиента. Поэтому requests и
lxml: выходит короче и честнее, чем обманывать SOAP-стек.

Весь период забирается ОДНИМ запросом: за восемь месяцев это ~100 КБ ответа,
дробить на дни незачем и невежливо по отношению к чужому сервису.

Курсы публикуются по рабочим дням, выходных и праздников в ответе нет. Это не
пробел: beancount берёт последнюю котировку НЕ ПОЗЖЕ нужной даты, поэтому
суббота считается по пятничному курсу — ровно так же, как считает банк.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
# Скрипт запускают файлом, поэтому в sys.path попадает tools/, а не корень.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Импорты ниже — после вставки в sys.path: без неё их не найти.
# pylint: disable=wrong-import-position
from finance.config import LEDGER  # noqa: E402

#: Путь берётся у общего конфига: при развёртывании леджер лежит на томе,
#: и FINANCE_LEDGER — то единственное место, где это сказано.
DEFAULT_OUTPUT = LEDGER / "prices.beancount"

#: Валюты, которые встречаются в леджере. Список задан явно, а не вычисляется
#: из проводок: лишняя пара директив `price` никому не мешает, а вот молчаливо
#: потерять валюту, появившуюся в выписке позже, было бы неприятно.
DEFAULT_CURRENCIES = ("RUB", "USD", "EUR")

HEADER = """\
;; Курсы валют к драму по данным Центробанка Армении.
;;
;; Файл собирается автоматически, править руками не нужно:
;;
;;     python tools/fetch_rates.py
;;
;; Период: {date_from} — {date_to}. Валюты: {codes}.
;; Котировки только по рабочим дням — так их публикует ЦБ. Beancount берёт
;; последнюю котировку не позже нужной даты, поэтому выходные считаются по
;; курсу предыдущего рабочего дня.
"""


class RatesError(Exception):
    """Курсы получить не удалось. Сообщение всегда объясняет, на чём именно."""


@dataclass(frozen=True)
class Quote:
    """Одна котировка: сколько драмов стоит единица валюты в этот день."""

    date: dt.date
    currency: str
    rate: Decimal

    def as_directive(self, base: str) -> str:
        # normalize() убирает хвостовые нули, но у круглых чисел оставляет
        # экспоненту («4E+1»), а её beancount не поймёт — отсюда :f.
        return f"{self.date.isoformat()} price {self.currency} {self.rate.normalize():f} {base}"


@dataclass(frozen=True)
class CbaRates:
    """Клиент к сервису курсов ЦБ Армении."""

    endpoint: str = "https://api.cba.am/exchangerates.asmx"
    soap_action: str = "http://www.cba.am/ExchangeRatesByDateRangeByISO"
    #: Валюта, против которой ЦБ котирует всё остальное.
    base: str = "AMD"
    timeout: int = 60

    ENVELOPE = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" \
xmlns:xsd="http://www.w3.org/2001/XMLSchema" \
xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <ExchangeRatesByDateRangeByISO xmlns="http://www.cba.am/">
      <ISOCodes>{codes}</ISOCodes>
      <DateFrom>{date_from}</DateFrom>
      <DateTo>{date_to}</DateTo>
    </ExchangeRatesByDateRangeByISO>
  </soap:Body>
</soap:Envelope>
"""

    def quotes(self, codes: list[str], date_from: dt.date, date_to: dt.date) -> list[Quote]:
        """Курсы за период, отсортированные по дате и валюте."""
        if date_from > date_to:
            raise RatesError("начало периода позже его конца")
        if not codes:
            raise RatesError("не указано ни одной валюты")
        if self.base in codes:
            raise RatesError(f"{self.base} — сама база котировок, курса к самой себе не бывает")

        parsed = self._parse(self._post(codes, date_from, date_to))
        if not parsed:
            raise RatesError("сервис вернул пустой список курсов — проверьте период и валюты")
        return sorted(parsed, key=lambda q: (q.date, q.currency))

    def _post(self, codes: list[str], date_from: dt.date, date_to: dt.date) -> bytes:
        body = self.ENVELOPE.format(
            codes=",".join(codes),
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
        ).encode("utf-8")
        try:
            response = requests.post(
                self.endpoint,
                data=body,
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f'"{self.soap_action}"',
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise RatesError(f"не достучаться до {self.endpoint}: {error}") from error
        return response.content

    def _parse(self, payload: bytes) -> list[Quote]:
        """Строки лежат в diffgram без пространства имён (на DocumentElement
        стоит xmlns=""), поэтому ищем по голому имени тега."""
        try:
            root = etree.fromstring(payload)
        except etree.XMLSyntaxError as error:
            raise RatesError(f"ответ сервиса не разбирается как XML: {error}") from error

        quotes = []
        for row in root.iter("ExchangeRatesByRange"):
            iso = row.findtext("ISO")
            rate = row.findtext("Rate")
            raw_date = row.findtext("RateDate")
            if not (iso and rate and raw_date):
                continue

            # `Amount` — за сколько единиц валюты дан курс. Для рубля, доллара
            # и евро это 1, но у йены и подобных бывает 100, и без деления курс
            # оказался бы завышен ровно в сто раз.
            try:
                amount = Decimal(row.findtext("Amount") or "1")
                value = Decimal(rate)
            except InvalidOperation as error:
                raise RatesError(f"{iso} на {raw_date[:10]}: курс не число — {rate!r}") from error
            if amount <= 0:
                continue

            # RateDate приходит с временем и поясом: «2026-08-03T00:00:00+04:00».
            quotes.append(Quote(dt.date.fromisoformat(raw_date[:10]), iso, value / amount))
        return quotes

    def as_directives(self, quotes: list[Quote]) -> str:
        return "\n".join(quote.as_directive(self.base) for quote in quotes) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Аргументы командной строки. Период по умолчанию — с начала года по сегодня."""
    today = dt.date.today()
    parser = argparse.ArgumentParser(
        description="Забрать курсы валют у ЦБ Армении и записать директивы `price`.",
        epilog=(
            "Файл перезаписывается целиком. Повторный запуск за тот же период "
            "даёт тот же результат, за более широкий — дополняет."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        type=dt.date.fromisoformat,
        default=today.replace(month=1, day=1),
        help="начало периода, ГГГГ-ММ-ДД (по умолчанию 1 января текущего года)",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        type=dt.date.fromisoformat,
        default=today,
        help="конец периода, ГГГГ-ММ-ДД (по умолчанию сегодня)",
    )
    parser.add_argument(
        "--currencies",
        default=",".join(DEFAULT_CURRENCIES),
        help=f"через запятую (по умолчанию {','.join(DEFAULT_CURRENCIES)})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"куда писать (по умолчанию {DEFAULT_OUTPUT.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="напечатать в стандартный вывод вместо записи в файл",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Забрать котировки за период и разложить их в директивы `price`."""
    options = parse_args(argv)
    codes = [code.strip().upper() for code in options.currencies.split(",") if code.strip()]

    client = CbaRates()
    try:
        quotes = client.quotes(codes, options.date_from, options.date_to)
    except RatesError as error:
        sys.exit(str(error))

    text = HEADER.format(
        date_from=options.date_from.isoformat(),
        date_to=options.date_to.isoformat(),
        codes=", ".join(codes),
    )
    text += "\n" + client.as_directives(quotes)

    if options.stdout:
        print(text)
        return

    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(text, encoding="utf-8")
    print(
        f"{options.output.relative_to(ROOT)}: {len(quotes)} котировок, "
        f"{len({quote.date for quote in quotes})} дат, "
        f"валюты {', '.join(sorted({quote.currency for quote in quotes}))}"
    )


if __name__ == "__main__":
    main()
