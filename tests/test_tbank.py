"""Тесты импортёра справок Т-Банка (.pdf)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data
from beangulp import extract

from finance.categorize import Rules
from finance.importers.tbank import Importer
from tests.conftest import check_golden
from tests.fixtures import RULES, TBANK_ACCOUNT, TBANK_DIR, TBANK_NUMBER, TBANK_NUMBER_OTHER
from tools.make_tbank_fixture import ROWS, Header, Row, build_statement, compute_totals

ROOT = Path(__file__).resolve().parents[1]
STATEMENT = TBANK_DIR / "statement.pdf"


@pytest.fixture(scope="module")
def rules() -> Rules:
    return Rules.load(RULES)


@pytest.fixture(scope="module")
def importer(rules) -> Importer:
    return Importer(TBANK_ACCOUNT, "RUB", TBANK_NUMBER, rules)


@pytest.fixture(scope="module")
def entries(importer) -> data.Entries:
    return importer.extract(str(STATEMENT), [])


@pytest.fixture(scope="module")
def transactions(entries) -> list[data.Transaction]:
    return [e for e in entries if isinstance(e, data.Transaction)]


def by_narration(transactions, text: str) -> data.Transaction:
    found = [
        t
        for t in transactions
        if text in (t.narration or "") or text in (t.meta.get("details") or "")
    ]
    assert len(found) == 1, f"ожидалась одна проводка с {text!r}, нашлось {len(found)}"
    return found[0]


def test_golden_file_matches():
    check_golden("tbank")


# ───────────────────────────── опознание файла ─────────────────────────────


def test_identifies_own_statement(importer):
    assert importer.identify(str(STATEMENT))
    assert importer.date(str(STATEMENT)).isoformat() == "2026-03-31"
    assert importer.filename(str(STATEMENT)) == "tbank-rub.pdf"


def test_account_number_from_file_selects_the_importer(rules):
    """Номер лицевого счёта лежит внутри PDF — по нему справка и достаётся счёту."""
    stranger = Importer("Assets:Tbank:Other", "RUB", TBANK_NUMBER_OTHER, rules)
    assert not stranger.identify(str(STATEMENT))


def test_broken_file_is_not_identified(importer, tmp_path):
    """identify() обязан вернуть False, а не уронить весь импорт."""
    garbage = tmp_path / "garbage.pdf"
    garbage.write_bytes(b"%PDF-1.5 and then nothing that makes sense")
    assert not importer.identify(str(garbage))

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert not importer.identify(str(empty))

    # Чужой формат: расширение отсеивается до чтения файла.
    assert not importer.identify(str(ROOT / "tests" / "acba" / "card" / "card.xls"))


def test_names_are_distinct(rules):
    """Имена импортёров различимы: fava отвергает конфиг с повторами."""
    first = Importer("Assets:Tbank:Rub", "RUB", TBANK_NUMBER, rules)
    second = Importer("Assets:Tbank:Savings", "RUB", TBANK_NUMBER_OTHER, rules)
    assert first.name != second.name


# ─────────────────────────────── разбор строк ───────────────────────────────


def test_every_row_of_the_fixture_is_extracted(transactions):
    """Повторная шапка таблицы и подвал не должны сойти за операции."""
    assert len(transactions) == len(ROWS)


def test_every_transaction_has_two_postings(transactions):
    for txn in transactions:
        assert len(txn.postings) == 2
        # Вторая нога без суммы: beancount выведет её сам.
        assert txn.postings[1].units is None


def test_amounts_and_dates_are_parsed(transactions):
    first = transactions[0]
    assert first.date.isoformat() == "2026-01-05"
    assert first.meta["time"] == "09:12"
    assert first.postings[0].units.number == Decimal("-1200.00")
    assert first.postings[0].units.currency == "RUB"

    # Разделитель тысяч — обычный пробел, и он не должен попасть в число.
    topup = by_narration(transactions, "Система быстрых платежей")
    assert topup.postings[0].units.number == Decimal("75000.00")


def test_operations_come_out_in_chronological_order(transactions):
    """Банк отдаёт справку от новых операций к старым, в леджер идёт наоборот."""
    dates = [t.date for t in transactions]
    assert dates == sorted(dates)


def test_foreign_currency_purchase_keeps_original_amount(transactions):
    """Списание в рублях, а покупка в драмах — обе суммы должны сохраниться."""
    delivery = by_narration(transactions, "DELIVERY ONE")
    assert delivery.postings[0].units.number == Decimal("-1250.75")
    assert delivery.meta["original"] == "-5400.00 AMD"

    # У рублёвой покупки добавлять нечего.
    assert "original" not in transactions[0].meta


def test_settlement_date_only_when_it_differs(transactions):
    delivery = by_narration(transactions, "DELIVERY ONE")
    assert delivery.meta["settlement"] == "2026-01-07"
    # Время списания отличается, а дата та же — писать нечего.
    assert "settlement" not in by_narration(transactions, "Autopay.Mobile").meta


def test_row_without_card_number_has_no_card_meta(transactions):
    """В строке кэшбэка банк ставит прочерк вместо номера карты."""
    cashback = by_narration(transactions, "Кэшбэк")
    assert "card" not in cashback.meta
    assert by_narration(transactions, "Autopay.Mobile").meta["card"] == "2070"


def test_multiline_description_is_joined(transactions):
    """Описание переносится по строкам, а в проводку идёт одной строкой."""
    external = by_narration(transactions, "Первый Банк")
    assert external.narration == (
        "Внешний банковский перевод счёт 20000000000000000002, "
        "Примерный филиал АО «Первый Банк»"
    )


def test_uncategorised_rows_are_flagged(transactions):
    for txn in transactions:
        uncategorised = txn.postings[1].account == "Expenses:Uncategorized"
        assert (txn.flag == "!") == uncategorised


def test_balance_assertion_comes_from_the_header(entries):
    """Остаток из шапки — на день после даты справки: balance смотрит начало дня."""
    balances = [e for e in entries if isinstance(e, data.Balance)]
    assert len(balances) == 1
    assert balances[0].date.isoformat() == "2026-04-01"
    assert balances[0].amount.number == Decimal("12345.67")


# ──────────────────────────────── дедупликация ────────────────────────────────


def test_operation_key_is_unique(transactions):
    keys = [t.meta["tbank-id"] for t in transactions]
    assert len(set(keys)) == len(keys)


def test_identical_operations_are_kept_apart(transactions):
    """Две неотличимые операции подряд — это две операции, а не одна.

    Своего номера у операций Т-Банка нет, поэтому их различает только
    порядковый номер в хвосте ключа.
    """
    twins = [t for t in transactions if t.postings[0].units.number == Decimal("-3.50")]
    assert len(twins) == 2
    assert {t.meta["tbank-id"][-2:] for t in twins} == {"-1", "-2"}


def test_reimport_marks_everything_as_duplicate(importer, entries, transactions):
    again = importer.extract(str(STATEMENT), entries)
    importer.deduplicate(again, entries)
    marked = [e for e in again if isinstance(e, data.Transaction) and extract.DUPLICATE in e.meta]
    assert len(marked) == len(transactions)


def test_only_the_overlapping_part_is_marked(importer, tmp_path):
    """Выписки внахлёст по датам: повторяются не все операции, а часть.

    Ровно ради этого случая дедуп и нужен — второй выгрузкой добираются новые
    операции, а старые не должны приехать во второй раз.
    """
    overlap = (ROWS[0], ROWS[2])
    short = build_statement(tmp_path / "short.pdf", rows=overlap)
    earlier = importer.extract(str(short), [])

    again = importer.extract(str(STATEMENT), earlier)
    importer.deduplicate(again, earlier)
    marked = [e for e in again if isinstance(e, data.Transaction) and extract.DUPLICATE in e.meta]
    assert len(marked) == len(overlap)


# ──────────────────────────── сверка с итогами ────────────────────────────


def test_totals_that_disagree_with_the_table_stop_the_import(rules, tmp_path):
    """Строка, потерянная разбором, обязана обрушить импорт, а не проехать молча.

    Подделываем то же самое с другой стороны: в таблице три операции, а итоги
    посчитаны по двум.
    """
    rows = (*ROWS[:2], Row("07.01.2026", "10:00", "07.01.2026", "10:00",
                           "-1.00 ₽", "-1.00 ₽", "Операция мимо итогов"))
    path = build_statement(tmp_path / "wrong.pdf", rows=rows, totals=compute_totals(rows[:-1]))

    importer = Importer(TBANK_ACCOUNT, "RUB", TBANK_NUMBER, rules)
    with pytest.raises(ValueError, match="Похоже, разбор потерял строки"):
        importer.extract(str(path), [])


def test_statement_without_totals_is_rejected(rules, tmp_path):
    """Без блока итогов сверять не с чем — это тоже повод остановиться."""
    path = build_statement(tmp_path / "no-totals.pdf", rows=ROWS[:2], totals=None)
    importer = Importer(TBANK_ACCOUNT, "RUB", TBANK_NUMBER, rules)
    with pytest.raises(ValueError, match="не найден блок итогов"):
        importer.extract(str(path), [])


# ───────────────────────────── защита от ошибок ─────────────────────────────


def test_unknown_currency_sign_is_rejected(rules, tmp_path):
    """Незнакомый знак валюты обязан упасть, а не уехать в валюту счёта."""
    row = Row("05.01.2026", "09:12", "05.01.2026", "09:12",
              "-10.00 $", "-10.00 $", "Оплата в FOREIGN ONE")
    path = build_statement(tmp_path / "usd.pdf", rows=(row,))
    importer = Importer(TBANK_ACCOUNT, "RUB", TBANK_NUMBER, rules)
    with pytest.raises(ValueError, match="знак валюты"):
        importer.extract(str(path), [])


def test_currency_mismatch_with_the_account_is_rejected(rules):
    """Справка в рублях не должна молча записаться на драмовый счёт."""
    importer = Importer("Assets:Tbank:Amd", "AMD", TBANK_NUMBER, rules)
    with pytest.raises(ValueError, match="не совпадает с валютой счёта"):
        importer.extract(str(STATEMENT), [])


def test_account_does_not_open_the_file(importer, tmp_path):
    """account() обязан отвечать не заглядывая в файл: так его зовёт beangulp."""
    assert importer.account(str(tmp_path / "нет-такого.pdf")) == TBANK_ACCOUNT


def test_fixture_font_covers_every_character():
    """Недостающий глиф уехал бы в notdef, и знак валюты пропал бы молча."""
    from tools.make_tbank_fixture import register_font

    assert register_font().exists()


def test_fixture_header_stays_in_placeholders():
    """Реквизиты фикстуры — заглушки: настоящих в ней быть не может."""
    header = Header()
    assert header.account.startswith("1000")
    assert "20000000000000000001" in header.footer[0]
