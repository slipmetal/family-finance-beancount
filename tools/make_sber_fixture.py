#!/usr/bin/env python3
"""Собрать тестовую выписку Сбербанка.

    python tools/make_sber_fixture.py

Пишет `tests/sber/statement.pdf`. Эталон после этого перегенерировать:

    python tests/golden.py sber generate --force

Как и фикстура Т-Банка, рисуется с нуля: настоящая выписка не участвует, поиск
шрифта и причины такого решения описаны в tools/make_tbank_fixture.py — здесь
они те же. Общий вывод: анонимизировать PDF нельзя проверяемо, потому что
tests/test_no_secrets читает файлы как текст и внутрь сжатых потоков не смотрит.

Воспроизводится геометрия настоящей выписки: размер страницы, левые края трёх
колонок, ПРАВЫЙ край колонки сумм (она прижата вправо), шаг строк, шапка на
каждой странице и перенос «Продолжение на следующей странице».

Набор операций задевает каждый разбираемый случай: приход со знаком плюс и
расход без знака, неразрывный пробел в тысячах, дата обработки на следующий
день, комиссия отдельной строкой, операция по карте и по счёту, разные
категории банка и разрыв страницы посреди таблицы.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from tools.make_tbank_fixture import FONT, register_font

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "sber" / "statement.pdf"

#: Геометрия настоящей выписки. На этих числах держится разбор
#: в finance/importers/sber.py — менять нельзя.
PAGE = (595, 842)
COL_DATE, COL_TIME, COL_TEXT = 45, 96, 145
#: Суммы прижаты к правому полю: у них фиксирован правый край, а не левый.
AMOUNT_RIGHT = 560
LINE_STEP = 11.0
ROW_GAP = 17.0
BOTTOM = 120

SIZE = 8.0
#: Неразрывный пробел — им банк разделяет тысячи. Задан через chr(), а не
#: буквально: в исходнике U+00A0 неотличим от обычного пробела, и правка
#: «лишнего» пробела сломала бы фикстуру молча.
NBSP = chr(0x00A0)


@dataclass(frozen=True)
class Row:
    """Операция. Суммы строками, чтобы фикстура задавала формат банка."""

    date: str
    time: str
    processed: str
    auth: str
    category: str
    #: Сумма как её печатает банк: расход без знака, приход с плюсом.
    amount: str
    details: str
    #: Комиссия, уже включённая в сумму. Пустая строка — примечания нет.
    fee: str = ""


@dataclass(frozen=True)
class Header:
    """Шапка выписки. Всё вымышлено, включая номера."""

    holder: str = "Иванова Мария Петровна"
    #: Номер печатается группами через пробел — на этом проверяется, что
    #: импортёр их убирает. Заглушка на 1000, как у остальных своих счетов:
    #: настоящий префикс 40817 tests/test_no_secrets.py принял бы за утечку.
    account: str = "10000 000 0 0000 0000003"
    card: str = "МИР Сберкарта •• 0001"
    currency: str = "Российский рубль"
    opened: str = "01.02.2019"
    period: tuple[str, str] = ("01.01.2026", "31.03.2026")
    issued: str = "31.03.2026"


ROWS = (
    Row("05.01.2026", "09:12", "05.01.2026", "100001", "Прочие расходы",
        "1" + NBSP + "200,00", "SUPERMARKET ONE MOSCOW RUS. Операция по карте ****0001"),
    Row("06.01.2026", "14:03", "07.01.2026", "100002", "Прочие расходы",
        "1" + NBSP + "250,75", "DELIVERY ONE MOSCOW RUS. Операция по карте ****0001"),
    Row("10.01.2026", "11:00", "10.01.2026", "100003", "Прочие операции",
        "+75" + NBSP + "000,00", "Заработная плата. Операция по счету ****0001"),
    # Комиссия отдельной строкой: она УЖЕ внутри суммы операции.
    Row("15.01.2026", "08:00", "15.01.2026", "100004", "Перевод с карты",
        "10" + NBSP + "037,50", "Перевод для И. Пётр Сергеевич. Операция по счету ****0001",
        fee="37,50"),
    Row("20.01.2026", "12:30", "20.01.2026", "100005", "Возврат, отмена операции",
        "+2" + NBSP + "581,00", "SUPERMARKET ONE MOSCOW RUS. Операция по карте ****0001"),
    Row("01.02.2026", "10:10", "01.02.2026", "100006", "Перевод СБП",
        "90" + NBSP + "000,00", "Перевод в T-Bank. Операция по счету ****0001"),
    Row("05.02.2026", "16:20", "06.02.2026", "100007", "Прочие расходы",
        "899,00", "BOOKSTORE ONE MOSCOW RUS. Операция по карте ****0001"),
    Row("12.02.2026", "21:05", "12.02.2026", "100008", "Оплата по QR–коду СБП",
        "450,00", "Оплата услуг. Операция по счету ****0001"),
    Row("01.03.2026", "13:00", "01.03.2026", "100009", "Перевод на карту",
        "+12" + NBSP + "000,00", "Перевод от И. Анна Петровна. Операция по счету ****0001"),
    Row("10.03.2026", "09:00", "10.03.2026", "100010", "Прочие расходы",
        "199,00", "оплата ж/д перевозок. Операция по карте ****0001"),
    Row("18.03.2026", "18:40", "18.03.2026", "100011", "Прочие операции",
        "303,58", "MAPP_SBERBANK_ONL@IN_PAY. Операция по счету ****0001", fee="3,01"),
    Row("25.03.2026", "07:00", "25.03.2026", "100012", "Прочие операции",
        "+62" + NBSP + "703,00", "Отпускные. Операция по счету ****0001"),
    Row("31.03.2026", "23:59", "31.03.2026", "100013", "Перевод с карты",
        "20" + NBSP + "000,00", "Перевод для К. Наталья Васильевна. Операция по счету ****0001"),
)


def needed_characters(rows=ROWS, header: Header | None = None) -> set[str]:
    """Все символы фикстуры — их покрытие шрифтом проверяется до отрисовки.

    Набор свой, а не тбанковский: здесь есть маркеры карты и короткое тире
    в «Оплата по QR–коду СБП», зато нет знаков рубля и драма.
    """
    h = header or Header()
    text = "".join(
        (
            h.holder, h.account, h.card, h.currency, h.opened, h.issued,
            *h.period,
            "900 www.sberbank.ru Заказано в СберБанк Онлайн",
            "ул. Вавилова, д. 19, Москва, 117312",
            "Индивидуальная выписка по платёжному счёту За период —",
            "ИТОГО ПО ОПЕРАЦИЯМ ЗА ПЕРИОД: Владелец счёта Номер счёта",
            "Карты, привязанные к счёту Валюта Дата открытия счёта",
            "Дата закрытия счёта - Расшифровка операций",
            "ДАТА ОПЕРАЦИИ (МСК) КАТЕГОРИЯ СУММА В ВАЛЮТЕ СЧЁТА",
            "Дата обработки1 Описание операции Сумма в валюте и код авторизации операции2",
            "Пополнение Списание Продолжение на следующей странице",
            "Дата формирования документа В сумму операции включена комиссия руб.",
            "ПАО Сбербанк. Генеральная лицензия Банка России "
            "на осуществление банковских операций № 0000 от 01.01.2015.",
            NBSP + "0123456789",
        )
    )
    for row in rows:
        text += "".join(
            (row.date, row.time, row.processed, row.auth,
             row.category, row.amount, row.details, row.fee)
        )
    return set(text)


@dataclass
class _Sheet:
    pdf: canvas.Canvas
    header: Header
    page: int = 1
    y: float = 0.0

    def text(self, x: float, y: float, value: str, size: float = SIZE) -> None:
        self.pdf.setFont(FONT, size)
        self.pdf.drawString(x, y, value)

    def right(self, y: float, value: str, size: float = SIZE) -> None:
        """Прижать текст к правому полю — так банк печатает суммы."""
        self.pdf.setFont(FONT, size)
        self.pdf.drawString(AMOUNT_RIGHT - self.width(value, size), y, value)

    def width(self, value: str, size: float = SIZE) -> float:
        return pdfmetrics.stringWidth(value, FONT, size)


def build_statement(path: Path, rows=ROWS, header: Header | None = None, totals="auto") -> Path:
    """Нарисовать выписку и вернуть путь к ней.

    `totals`: `"auto"` — посчитать обороты по строкам, пара строк — поставить
    свои (так проверяется, что разбор ловит расхождение), `None` — не рисовать
    блок итогов вовсе.
    """
    if FONT not in pdfmetrics.getRegisteredFontNames():
        register_font(needed_characters())
    header = header or Header()
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=PAGE)
    sheet = _Sheet(pdf, header)

    _first_page_header(sheet, compute_totals(rows) if totals == "auto" else totals)
    _table_header(sheet, y=top(344.3))
    sheet.y = top(394.3)

    for row in reversed(rows):  # банк печатает от новых операций к старым
        if sheet.y - _row_height(row) < BOTTOM:
            _new_page(sheet)
        _draw_row(sheet, row)

    _document_footer(sheet)
    pdf.save()
    return path


def top(value: float) -> float:
    """Координаты сняты с настоящей выписки сверху, reportlab считает снизу."""
    return PAGE[1] - value


def _first_page_header(sheet: _Sheet, totals) -> None:
    h = sheet.header
    sheet.text(171, top(22.3), "900")
    sheet.text(191, top(22.3), "www.sberbank.ru")
    sheet.text(452, top(22.3), "Заказано в СберБанк Онлайн")
    sheet.text(171, top(38.3), "ул. Вавилова, д. 19, Москва, 117312")

    sheet.text(45, top(68.7), "Индивидуальная выписка по платёжному счёту", size=13)
    start, end = h.period
    sheet.text(45, top(94.6), f"За период {start} — {end}", size=9)

    sheet.text(349, top(123.6), "ИТОГО ПО ОПЕРАЦИЯМ ЗА ПЕРИОД:")
    sheet.text(57, top(130.3), "Владелец счёта")
    sheet.text(57, top(142.7), h.holder, size=9)

    if totals is not None:
        credit, debit = totals
        sheet.text(349, top(151.3), "Пополнение")
        sheet.right(top(151.3), f"+{credit}")
        sheet.text(349, top(167.3), "Списание")
        sheet.right(top(167.3), debit)

    sheet.text(57, top(167.3), "Номер счёта")
    sheet.text(195, top(167.3), h.account)
    sheet.text(57, top(183.3), "Карты, привязанные к счёту")
    sheet.text(195, top(183.3), h.card)
    sheet.text(57, top(199.3), "Валюта")
    sheet.text(195, top(199.3), h.currency)
    sheet.text(57, top(215.3), "Дата открытия счёта")
    sheet.text(195, top(215.3), h.opened)
    sheet.text(57, top(231.3), "Дата закрытия счёта")
    sheet.text(195, top(231.3), "-")

    sheet.text(45, top(316.5), "Расшифровка операций", size=11)


def _table_header(sheet: _Sheet, y: float) -> None:
    """Шапка таблицы: три строки, повторяется на каждой странице.

    Подписи стоят по левым краям, снятым с настоящей выписки, а не выровнены
    вправо: у банка «СУММА В ВАЛЮТЕ СЧЁТА» начинается ровно на 469, и по этому
    краю видно, где проходит граница колонки сумм. Размер меньше основного —
    иначе Arial шире банковского шрифта и подпись вылезает за поле.
    """
    sheet.text(COL_DATE, y, "ДАТА ОПЕРАЦИИ (МСК)")
    sheet.text(COL_TEXT, y, "КАТЕГОРИЯ")
    sheet.text(469, y, "СУММА В ВАЛЮТЕ СЧЁТА", size=7)
    sheet.text(COL_DATE, y - 14.3, "Дата обработки1")
    sheet.text(COL_TEXT, y - 15.0, "Описание операции")
    sheet.text(504, y - 15.0, "Сумма в валюте", size=7)
    sheet.text(COL_DATE, y - 24.6, "и код авторизации")
    sheet.text(523, y - 23.9, "операции2", size=7)


def _new_page(sheet: _Sheet) -> None:
    # Ниже последней строки, а не на фиксированной высоте: на этой пометке
    # разбор обрывает таблицу, и окажись она выше — потерялась бы операция.
    sheet.text(236, sheet.y - ROW_GAP, "Продолжение на следующей странице")
    sheet.pdf.showPage()
    sheet.page += 1
    _table_header(sheet, y=top(60.0))
    sheet.y = top(110.0)


def _row_height(row: Row) -> float:
    return LINE_STEP + ROW_GAP + (LINE_STEP if row.fee else 0)


def _draw_row(sheet: _Sheet, row: Row) -> None:
    y = sheet.y
    sheet.text(COL_DATE, y, row.date)
    sheet.text(COL_TIME, y, row.time)
    sheet.text(COL_TEXT, y, row.category)
    sheet.right(y, row.amount)

    sheet.text(COL_DATE, y - LINE_STEP, row.processed)
    sheet.text(COL_TIME, y - LINE_STEP, row.auth)
    sheet.text(COL_TEXT, y - LINE_STEP, row.details)

    if row.fee:
        sheet.text(COL_TEXT, y - 2 * LINE_STEP,
                   f"В сумму операции включена комиссия {row.fee} руб.")

    sheet.y = y - _row_height(row)


def _document_footer(sheet: _Sheet) -> None:
    """Реквизиты документа. На них разбор останавливает таблицу."""
    y = min(sheet.y - ROW_GAP, top(445.3))
    sheet.text(45, y, "Дата формирования документа")
    sheet.text(189.8, y, sheet.header.issued)
    sheet.text(426, y - 15.5, "00000000000000000000000000000000")
    sheet.text(45, y - 91.3,
               "ПАО Сбербанк. Генеральная лицензия Банка России "
               "на осуществление банковских операций № 0000 от 01.01.2015.")


def compute_totals(rows) -> tuple[str, str]:
    """Обороты за период. Знак несёт подпись, сами числа даны по модулю."""
    credit = sum((_value(r.amount) for r in rows if _value(r.amount) > 0), Decimal(0))
    debit = -sum((_value(r.amount) for r in rows if _value(r.amount) < 0), Decimal(0))
    return _format(credit), _format(debit)


def _value(amount: str) -> Decimal:
    plain = amount.replace(NBSP, "").replace(" ", "").replace(",", ".")
    return Decimal(plain[1:]) if plain.startswith("+") else -Decimal(plain)


def _format(value: Decimal) -> str:
    whole, _, cents = f"{value:.2f}".partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return f"{NBSP.join(groups)},{cents}"


if __name__ == "__main__":
    used = register_font(needed_characters())
    written = build_statement(OUT)
    print(f"{written.relative_to(ROOT)}: {len(ROWS)} операций, шрифт {used}")
    print("Дальше: python tests/golden.py sber generate --force", file=sys.stderr)
