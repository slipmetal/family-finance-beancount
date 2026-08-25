#!/usr/bin/env python3
"""Собрать тестовую выписку Т-Банка.

    python tools/make_tbank_fixture.py

Пишет `tests/tbank/statement.pdf`. Эталон после этого перегенерировать:

    python tbank_test.py generate tests/tbank --force

В отличие от фикстур ACBA, здесь **настоящая выписка не участвует вовсе**:
tools/anonymize.py не нужен, карту замен искать не надо. Причина в формате.
Анонимизировать PDF значит переписать текст внутри сжатых потоков, а проверка
tests/test_no_secrets.py читает файлы как текст и внутрь потоков не заглядывает —
недосмотр в такой анонимизации остался бы незамеченным. Поэтому данные
придуманы целиком, и утечки нет по построению.

Что воспроизводится от настоящей справки — это геометрия: размер страницы,
левые края колонок, шаг строк, повторяющаяся на каждой странице шапка таблицы,
подвал с реквизитами и отдельная страница с итогами. Именно на них держится
разбор, и именно они должны быть под тестом.

Набор операций подобран так, чтобы задеть каждый разбираемый случай: покупка
в чужой валюте, описание на несколько строк, строка без номера карты, приход,
разделитель тысяч, дата списания на следующий день, две неотличимые операции
подряд и разрыв страницы посреди таблицы.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from reportlab.lib.colors import black
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "tbank" / "statement.pdf"

#: Геометрия настоящей справки: страница, колонки, шаги. Менять нельзя —
#: на этих числах держится разбор в finance/importers/tbank.py.
PAGE = (595, 842)
COLUMNS = (56, 126, 199, 294, 389, 499)
#: Ширина колонки описания: в настоящей справке текст не заходит правее 493.
DETAILS_WIDTH = 104
LINE_STEP = 11.08
ROW_GAP = 13.92
#: Ниже этой отметки строки не ставятся — там подвал.
BOTTOM = 100

FONT = "Statement"
SIZE = 8.5

#: Шрифт ищется среди системных: свой в репозиторий класть незачем, генератор
#: гоняется руками и на CI не запускается.
FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
)


@dataclass(frozen=True)
class Row:
    """Строка таблицы. Суммы строками, чтобы фикстура задавала формат банка."""

    date: str
    time: str
    settlement: str
    settlement_time: str
    #: Сумма в валюте операции со знаком валюты: `-5 400.00 Դ`.
    original: str
    #: Сумма в валюте счёта, всегда в рублях.
    amount: str
    details: str
    card: str = "1580"


@dataclass(frozen=True)
class Header:
    """Шапка первой страницы. Всё вымышлено, включая номера."""

    reference: str = "00000000"
    issued: str = "31.03.2026"
    holder: str = "Иванова Мария Петровна"
    address: str = "100000, Примерная Обл, Г Примерск, Ул Первая , д. 1, кв. 1"
    opened: str = "01.02.2019"
    contract: str = "1000000001"
    account: str = "10000000000000000001"
    balance: str = "12 345.67 ₽"
    period: tuple[str, str] = ("01.01.2026", "31.03.2026")
    #: Реквизиты банка в подвале — тоже заглушки: тест на утечки не отличает
    #: публичный корсчёт от личного, он смотрит только на форму числа.
    footer: tuple[str, str] = (
        "АО «ТБанк» универсальная лицензия Банка России № 0000, "
        "к/с 20000000000000000001 в ГУ Банка России по ЦФО",
        "БИК 000000000 ИНН 0000000000 КПП 000000000",
    )
    bank: tuple[str, str, str] = (
        "АКЦИОНЕРНОЕ ОБЩЕСТВО «ТБАНК»",
        "РОССИЯ, 000000, МОСКВА, УЛ. ПРИМЕРНАЯ, Д. 1",
        "ТЕЛ.: +7 000 000-00-00,  TBANK.RU",
    )


ROWS = (
    Row("05.01.2026", "09:12", "05.01.2026", "09:30",
        "-1 200.00 ₽", "-1 200.00 ₽", "Оплата в SUPERMARKET ONE Moskva RUS"),
    # Покупка в Армении рублёвой картой: две валюты и списание на следующий день.
    Row("06.01.2026", "14:03", "07.01.2026", "06:15",
        "-5 400.00 Դ", "-1 250.75 ₽", "Оплата в DELIVERY ONE EREVAN ARM"),
    Row("10.01.2026", "11:00", "10.01.2026", "11:00",
        "+75 000.00 ₽", "+75 000.00 ₽", "Пополнение. Система быстрых платежей"),
    # Две неотличимые операции подряд: у ключа дедупа должен появиться номер.
    Row("12.02.2026", "08:00", "12.02.2026", "08:12",
        "-3.50 ₽", "-3.50 ₽", "Перевод для пополнения счета Накопилка"),
    Row("12.02.2026", "08:00", "12.02.2026", "08:12",
        "-3.50 ₽", "-3.50 ₽", "Перевод для пополнения счета Накопилка"),
    Row("15.02.2026", "19:45", "16.02.2026", "03:20",
        "-10 000.00 ₽", "-10 000.00 ₽",
        "Внешний банковский перевод счёт 20000000000000000002, "
        "Примерный филиал АО «Первый Банк»"),
    # Кэшбэк приходит без номера карты — банк ставит там прочерк.
    Row("20.02.2026", "12:30", "20.02.2026", "12:30",
        "+250.00 ₽", "+250.00 ₽", "Кэшбэк за обычные покупки", card="—"),
    Row("01.03.2026", "10:10", "01.03.2026", "10:41",
        "-899.00 ₽", "-899.00 ₽", "Оплата услуг Autopay.Mobile", card="2070"),
    # Идентификатор договора банк рвёт посреди слова — описание на пять строк.
    Row("05.03.2026", "16:20", "05.03.2026", "16:20",
        "-2 500.00 ₽", "-2 500.00 ₽",
        "Сберегательный взнос по договору "
        "c=1000000001;o=00000000-0000-4000-8000-000000000001"),
    Row("10.03.2026", "21:05", "10.03.2026", "21:30",
        "-450.00 ₽", "-450.00 ₽", "Оплата в BOOKSTORE ONE Moskva RUS"),
    Row("15.03.2026", "13:00", "15.03.2026", "13:00",
        "+12 000.00 ₽", "+12 000.00 ₽", "Внутрибанковский перевод с договора 1000000002"),
    Row("18.03.2026", "09:00", "18.03.2026", "09:00",
        "-199.00 ₽", "-199.00 ₽", "Комиссия за перевод денежных средств"),
    Row("25.03.2026", "18:40", "25.03.2026", "19:02",
        "-7 300.00 Դ", "-1 690.20 ₽", "Оплата в TAXI ONE EREVAN ARM"),
    Row("31.03.2026", "07:00", "31.03.2026", "07:15",
        "-20 000.00 ₽", "-20 000.00 ₽", "Внутренний перевод на договор 1000000003"),
)


# ─────────────────────────────── шрифт ───────────────────────────────


def register_font(candidates=FONT_CANDIDATES) -> Path:
    """Найти шрифт, в котором есть все символы фикстуры, и зарегистрировать его.

    Покрытие проверяется явно. Без проверки недостающий глиф молча уехал бы
    в notdef: знак рубля или драма пропал бы из PDF, а разбор валюты — вместе
    с ним, причём эталон при этом перегенерировался бы без единой жалобы.
    """
    needed = _needed_characters()
    tried = []
    for path in candidates:
        if not path.exists():
            continue
        font = TTFont(FONT, str(path))
        missing = sorted(ch for ch in needed if ord(ch) not in font.face.charToGlyph)
        if missing:
            tried.append(f"{path}: нет символов {' '.join(missing)}")
            continue
        pdfmetrics.registerFont(font)
        return path
    raise SystemExit(
        "не нашлось шрифта, покрывающего кириллицу, знак рубля и знак драма.\n"
        + ("\n".join(f"  {line}" for line in tried) or "  ни одного файла не найдено")
        + "\nПроверенные пути: "
        + ", ".join(str(p) for p in candidates)
    )


def _needed_characters() -> set[str]:
    header = Header()
    text = "".join(
        (
            *header.bank,
            *header.footer,
            *header.period,
            header.reference, header.issued, header.holder, header.address,
            header.opened, header.contract, header.account, header.balance,
            "Справка о движении средств Исх. № Адрес места жительства: О продукте",
            "Дата заключения договора: Номер лицевого счета: Номер договора:",
            "Сумма доступного остатка на Движение средств за период с по",
            "Дата и время операции списания Сумма в валюте Сумма операции",
            "в валюте карты Описание Номер карты Пополнения: Расходы:",
            "С уважением, Руководитель Управления Бэк-офис И.И. Иванов",
        )
    )
    for row in ROWS:
        text += "".join(
            (row.date, row.time, row.settlement, row.settlement_time,
             row.original, row.amount, row.details, row.card)
        )
    return set(text)


# ─────────────────────────────── рисование ───────────────────────────────


@dataclass
class _Sheet:
    """Состояние отрисовки: холст, текущая страница и текущая высота."""

    pdf: canvas.Canvas
    header: Header
    page: int = 1
    y: float = 0.0

    def text(self, x: float, y: float, value: str, size: float = SIZE) -> None:
        self.pdf.setFont(FONT, size)
        self.pdf.setFillColor(black)
        self.pdf.drawString(x, y, value)

    def width(self, value: str, size: float = SIZE) -> float:
        return pdfmetrics.stringWidth(value, FONT, size)


def build_statement(path: Path, rows=ROWS, header: Header | None = None, totals="auto") -> Path:
    """Нарисовать справку и вернуть путь к ней.

    Вынесено отдельной функцией, чтобы тесты могли собирать варианты выписки
    во временном каталоге. `totals` для того же: `"auto"` — посчитать обороты
    по строкам, пара строк — поставить свои (так проверяется, что разбор ловит
    расхождение), `None` — не рисовать блок итогов вовсе.
    """
    if FONT not in pdfmetrics.getRegisteredFontNames():
        register_font()
    header = header or Header()
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=PAGE)
    sheet = _Sheet(pdf, header)

    _first_page_header(sheet)
    _table_header(sheet, y=518.8)
    sheet.y = 491.8

    for row in reversed(rows):  # банк отдаёт справку от новых операций к старым
        height = _row_height(sheet, row)
        if sheet.y - height < BOTTOM:
            _new_page(sheet)
        _draw_row(sheet, row)

    _totals_page(sheet, compute_totals(rows) if totals == "auto" else totals)
    _page_footer(sheet)
    pdf.save()
    return path


def _first_page_header(sheet: _Sheet) -> None:
    header = sheet.header
    for offset, line in enumerate(header.bank):
        sheet.text(314, 802.24 - offset * 10.46, line)
    sheet.text(56, 736.55, "Справка о движении средств", size=20)
    sheet.text(56, 712.87, f"Исх. № {header.reference}", size=10)
    sheet.text(492.44, 712.87, header.issued, size=10)
    sheet.text(56, 690.82, header.holder, size=9)

    sheet.pdf.setLineWidth(0.5)
    sheet.pdf.line(56, 686, 539, 686)

    sheet.text(56, 672.82, f"Адрес места жительства:  {header.address}", size=9)
    sheet.text(56, 643.8, "О продукте", size=9)
    sheet.text(56, 620.8, f"Дата заключения договора:  {header.opened}", size=9)
    sheet.text(56, 602.8, f"Номер договора:  {header.contract}", size=9)
    sheet.text(56, 584.8, f"Номер лицевого счета:  {header.account}", size=9)
    sheet.text(56, 566.8, f"Сумма доступного остатка на {header.issued}:  {header.balance}", size=9)
    start, end = header.period
    sheet.text(56, 542.8, f"Движение средств за период с {start} по {end}", size=9)


def _table_header(sheet: _Sheet, y: float) -> None:
    top = ("Дата и время", "Дата", "Сумма в валюте", "Сумма операции", "Описание", "Номер")
    bottom = ("операции", "списания", "операции", "в валюте карты", "операции", "карты")
    for column, value in zip(COLUMNS, top):
        sheet.text(column, y, value)
    for column, value in zip(COLUMNS, bottom):
        sheet.text(column, y - LINE_STEP, value)


def _page_footer(sheet: _Sheet) -> None:
    first, second = sheet.header.footer
    sheet.text((PAGE[0] - sheet.width(first)) / 2, 52.7, first)
    sheet.text((PAGE[0] - sheet.width(second)) / 2, 40.4, second)
    sheet.text(535, 32.7, str(sheet.page))


def _new_page(sheet: _Sheet) -> None:
    _page_footer(sheet)
    sheet.pdf.showPage()
    sheet.page += 1
    _table_header(sheet, y=813.8)
    sheet.y = 786.8


def _wrap(sheet: _Sheet, text: str) -> list[str]:
    """Разложить описание по ширине колонки — так же, как это делает банк.

    Слово, которое само шире колонки (идентификатор договора), режется по
    символам: банк поступает ровно так же.
    """
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        while sheet.width(word) > DETAILS_WIDTH:
            cut = len(word)
            while cut > 1 and sheet.width(word[:cut]) > DETAILS_WIDTH:
                cut -= 1
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:cut])
            word = word[cut:]
        candidate = f"{current} {word}".strip()
        if current and sheet.width(candidate) > DETAILS_WIDTH:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _row_height(sheet: _Sheet, row: Row) -> float:
    lines = max(2, len(_wrap(sheet, row.details)))
    return (lines - 1) * LINE_STEP + ROW_GAP


def _draw_row(sheet: _Sheet, row: Row) -> None:
    y = sheet.y
    sheet.text(COLUMNS[0], y, row.date)
    sheet.text(COLUMNS[1], y, row.settlement)
    _amount(sheet, COLUMNS[2], y, row.original)
    _amount(sheet, COLUMNS[3], y, row.amount)
    sheet.text(COLUMNS[5], y, row.card)

    sheet.text(COLUMNS[0], y - LINE_STEP, row.time)
    sheet.text(COLUMNS[1], y - LINE_STEP, row.settlement_time)
    for offset, line in enumerate(_wrap(sheet, row.details)):
        sheet.text(COLUMNS[4], y - offset * LINE_STEP, line)

    sheet.y = y - _row_height(sheet, row)


def _amount(sheet: _Sheet, x: float, y: float, value: str) -> None:
    """Сумма и знак валюты — двумя кусками, знак чуть выше.

    В настоящей справке знак рубля и знак драма приходят из другого шрифта и
    садятся примерно на пункт выше остальной строки. Расхождение воспроизводится
    намеренно: на нём проверяется допуск, с которым разбор собирает строку.

    Пробел перед знаком входит в саму сумму — так же, как у банка.
    """
    number, sign = value.rsplit(" ", 1)
    number += " "
    sheet.text(x, y, number)
    sheet.text(x + sheet.width(number), y + 0.92, sign)


def _totals_page(sheet: _Sheet, totals: tuple[str, str] | None) -> None:
    """Итоговая страница. В настоящей справке она отдельная, без таблицы."""
    _page_footer(sheet)
    sheet.pdf.showPage()
    sheet.page += 1

    if totals is not None:
        credit, debit = totals
        sheet.text(COLUMNS[0], 811.5, "Пополнения:")
        _amount(sheet, COLUMNS[1], 811.5, credit)
        sheet.text(COLUMNS[0], 796.5, "Расходы:")
        _amount(sheet, COLUMNS[1], 796.5, debit)

    sheet.text(56, 735.9, "С уважением,")
    sheet.text(56, 719.0, "Руководитель Управления Бэк-офис")
    sheet.text(446, 717.6, "И.И. Иванов")


def compute_totals(rows) -> tuple[str, str]:
    """Обороты за период — банк печатает их с запятой вместо точки."""
    credit = sum((_value(row.amount) for row in rows if _value(row.amount) > 0), Decimal(0))
    debit = -sum((_value(row.amount) for row in rows if _value(row.amount) < 0), Decimal(0))
    return _format(credit), _format(debit)


def _value(amount: str) -> Decimal:
    return Decimal(re.sub(r"[^\d.+-]", "", amount))


def _format(value: Decimal) -> str:
    whole, _, cents = f"{value:.2f}".partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return f"{' '.join(groups)},{cents} ₽"


if __name__ == "__main__":
    used = register_font()
    written = build_statement(OUT)
    print(f"{written.relative_to(ROOT)}: {len(ROWS)} операций, шрифт {used}")
    print("Дальше: python tbank_test.py generate tests/tbank --force", file=sys.stderr)
