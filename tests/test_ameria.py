"""Тесты импортёра Ameriabank.

Regression-тест на эталонном файле живёт в beangulp и запускается своей
click-командой, которую pytest сам не подхватит — зовём её подпроцессом,
чтобы `pytest` прогонял всё разом.
"""

from __future__ import annotations

import csv
from decimal import Decimal

import pytest

from finance.categorize import Rules
from finance.importers.ameria import CardImporter, _clean_narration
from tests.conftest import check_golden
from tests.fixtures import (
    AMERIA_ACCOUNT,
    AMERIA_ACCOUNT_OTHER,
    AMERIA_CARD_DIR,
    AMERIA_MARKER,
    AMERIA_MARKER_OTHER,
    RULES,
)

FIXTURES = AMERIA_CARD_DIR
# Метка счёта в имени обязательна — по ней импортёр и опознаёт файл.
STATEMENT = FIXTURES / f"{AMERIA_MARKER}_statement.csv"


@pytest.fixture(scope="module")
def rules() -> Rules:
    return Rules.load(RULES)


@pytest.fixture(scope="module")
def importer(rules) -> Importer:
    return CardImporter(AMERIA_ACCOUNT, "AMD", rules, marker=AMERIA_MARKER)


def test_golden_file_matches():
    """Эталон покрывает разбор строк плюс account(), date() и filename()."""
    check_golden("ameria-card")


def test_identifies_own_statement(importer):
    assert importer.identify(str(STATEMENT))


def test_rejects_foreign_csv(importer, tmp_path):
    """Чужой CSV не должен опознаваться: иначе beangulp упадёт на конфликте."""
    other = tmp_path / "other.csv"
    other.write_text('"Date","Amount","Description"\n"2026-01-01","10","x"\n', encoding="utf-8")
    assert not importer.identify(str(other))

    not_csv = tmp_path / "statement.txt"
    not_csv.write_text(STATEMENT.read_text(encoding="utf-8-sig"), encoding="utf-8")
    assert not importer.identify(str(not_csv))


def test_marker_in_filename_selects_the_account(rules, tmp_path):
    """Две карты в AMD различимы только по метке: номера счёта в файле нет.

    Метка, а не папка: fava складывает всё загруженное через браузер в одну
    папку, и при маршрутизации по папкам выписка второй карты молча досталась
    бы первой.
    """
    raw = STATEMENT.read_text(encoding="utf-8-sig")
    mine = CardImporter(AMERIA_ACCOUNT, "AMD", rules, marker=AMERIA_MARKER)
    other = CardImporter(AMERIA_ACCOUNT_OTHER, "AMD", rules, marker=AMERIA_MARKER_OTHER)

    # Один и тот же каталог — различает только имя файла.
    for name, owner, stranger in [
        (f"{AMERIA_MARKER}_2026-08.csv", mine, other),
        (f"{AMERIA_MARKER_OTHER.upper()}-june.csv", other, mine),
    ]:
        path = tmp_path / name
        path.write_text(raw, encoding="utf-8-sig")
        assert owner.identify(str(path)), name
        assert not stranger.identify(str(path)), name

    # Без метки файл не достаётся никому — лучше, чем достаться не тому.
    # Цифры в имени начинаются с нулей не случайно: это заглушка, иначе длинное
    # число тут ловит tests/test_no_secrets.py как похожее на номер счёта.
    plain = tmp_path / "export_00001643941300036250.csv"
    plain.write_text(raw, encoding="utf-8-sig")
    assert not mine.identify(str(plain))
    assert not other.identify(str(plain))


def test_unique_currency_identifies_the_file_without_a_marker(rules, tmp_path):
    """Единственную карту в своей валюте переименовывать незачем.

    Признак считает finance/config.py: ему видны все счета сразу. Здесь только
    проверяется, что импортёр им пользуется.
    """
    raw = STATEMENT.read_text(encoding="utf-8-sig")
    plain = tmp_path / "export_00001643941300036250.csv"
    plain.write_text(raw, encoding="utf-8-sig")

    alone = CardImporter(
        AMERIA_ACCOUNT, "AMD", rules, marker=AMERIA_MARKER, marker_optional=True
    )
    assert alone.identify(str(plain))

    # Валюта и здесь право вето: чужая выписка не достаётся никому.
    foreign = CardImporter(
        "Assets:Ameria:Rub", "RUB", rules, marker="rub", marker_optional=True
    )
    assert not foreign.identify(str(plain))


def test_empty_statement_needs_a_marker_even_when_alone(rules, tmp_path):
    """У выписки без операций валюты не видно — верить нечему, кроме имени.

    Иначе безымянный пустой файл достался бы первому же счёту, которому метка
    не нужна, просто потому что опровергнуть это нечем.
    """
    header = STATEMENT.read_text(encoding="utf-8-sig").splitlines()[0]
    named = tmp_path / f"{AMERIA_MARKER}_empty.csv"
    named.write_text(header + "\n", encoding="utf-8-sig")
    plain = tmp_path / "export_00001643941300036251.csv"
    plain.write_text(header + "\n", encoding="utf-8-sig")

    imp = CardImporter(
        AMERIA_ACCOUNT, "AMD", rules, marker=AMERIA_MARKER, marker_optional=True
    )
    assert imp.identify(str(named))
    assert not imp.identify(str(plain))


def test_currency_vetoes_a_mislabelled_file(rules, tmp_path):
    """Ошиблись меткой — выручает проверка валюты по содержимому."""
    in_rubles = tmp_path / f"{AMERIA_MARKER}_oops.csv"
    in_rubles.write_text(
        STATEMENT.read_text(encoding="utf-8-sig").replace("AMD", "RUB"), encoding="utf-8-sig"
    )
    amd = CardImporter(AMERIA_ACCOUNT, "AMD", rules, marker=AMERIA_MARKER)
    assert not amd.identify(str(in_rubles))


def test_foreign_currency_purchase_keeps_original_amount(importer):
    """У покупки за рубежом колонки 6 и 7 расходятся: USD списан, AMD снят."""
    entries = importer.extract(str(STATEMENT), [])
    apple = next(e for e in entries if e.payee == "Apple")

    assert apple.postings[0].units.number == Decimal("-1329.69")
    assert apple.postings[0].units.currency == "AMD"
    assert apple.meta["original"] == "-3.49 USD"


def test_amounts_and_dates_are_parsed(importer):
    """Суммы с разделителем тысяч и кодом валюты, даты с двузначным годом."""
    entries = importer.extract(str(STATEMENT), [])
    assert len(entries) == 28

    first = entries[1]
    assert first.date.isoformat() == "2026-01-19"
    assert first.postings[0].units.number == Decimal("2500.00")
    assert first.postings[0].units.currency == "AMD"
    assert first.meta["time"] == "14:12"

    # Сумма разобранных проводок сходится с колонкой выписки до копейки.
    expected = sum(
        Decimal(row["Transaction amount in account currency"].replace(",", "").removesuffix(" AMD"))
        for row in csv.DictReader(STATEMENT.open(encoding="utf-8-sig"))
    )
    assert sum(e.postings[0].units.number for e in entries) == expected


def test_every_transaction_has_two_postings(importer):
    entries = importer.extract(str(STATEMENT), [])
    for entry in entries:
        assert len(entry.postings) == 2, entry
        # Вторая нога без суммы: beancount добивает её сам, поэтому арифметика
        # проводки не может разойтись.
        assert entry.postings[1].units is None


def test_uncategorised_rows_are_flagged(importer):
    """Строки без правила помечаются `!`, всё остальное — `*`."""
    entries = importer.extract(str(STATEMENT), [])
    for entry in entries:
        uncategorised = entry.postings[1].account == "Expenses:Uncategorized"
        assert (entry.flag == "!") == uncategorised, entry


def test_identical_rows_are_kept(importer):
    """Две одинаковые SMS-комиссии в одну минуту — это две разные операции.

    ID транзакции в выписке нет, поэтому схлопывание по содержимому потеряло бы
    реальные данные.
    """
    entries = importer.extract(str(STATEMENT), [])
    sms = [e for e in entries if e.postings[1].account == "Expenses:Fees:Bank:SMS"]
    assert len(sms) == 2
    assert sms[0].date == sms[1].date
    assert sms[0].postings[0].units == sms[1].postings[0].units


def test_settlement_date_only_when_it_differs(importer):
    entries = importer.extract(str(STATEMENT), [])
    by_narration = {e.narration: e for e in entries}

    deferred = by_narration["911 PHARM LLC YEREVAN AM 544538"]
    assert deferred.meta["settlement"].isoformat() == "2026-02-02"

    same_day = by_narration["Перевод собственных средств"]
    assert "settlement" not in same_day.meta


def test_raw_values_are_preserved_in_metadata(importer):
    """Правило подменяет payee и narration — сырые значения не теряются."""
    entries = importer.extract(str(STATEMENT), [])
    taxi = next(e for e in entries if e.postings[1].account == "Expenses:Transport:Taxi")

    assert taxi.payee == "Yandex Go"
    assert taxi.meta["counterparty"] == "«ՐԱՅԴԹԵՔ ԷՅ ԷՄ» ՍՊԸ"
    assert taxi.meta["details"] == "Ք: YANDEX. GO YEREVAN AM 180727"
    assert taxi.meta["bank-type"] == "Քարտային գործարք"


def test_foreign_currency_row_is_rejected(importer, tmp_path):
    """Мультивалютная выписка должна падать внятно, а не писать доллары как драмы."""
    lines = STATEMENT.read_text(encoding="utf-8-sig").splitlines()
    lines[1] = lines[1].replace("AMD", "USD")
    broken = tmp_path / "usd.csv"
    broken.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig", newline="")

    with pytest.raises(ValueError, match="USD"):
        importer.extract(str(broken), [])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ք: SAS YEREVAN AM 578094", "SAS YEREVAN AM 578094"),
        ("Transfer of own funds", "Transfer of own funds"),
        ("  Ք: ATM  335   MASHTOTS ", "ATM 335 MASHTOTS"),
    ],
)
def test_card_prefix_is_stripped(raw, expected):
    assert _clean_narration(raw) == expected