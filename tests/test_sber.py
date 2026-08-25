"""Тесты импортёра выписок Сбербанка (.pdf)."""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data
from beangulp import extract

from finance.categorize import Rules
from finance.importers.sber import Importer
from tests.fixtures import RULES, SBER_ACCOUNT, SBER_DIR, SBER_NUMBER, SBER_NUMBER_OTHER
from tools.make_sber_fixture import ROWS, Header, Row, build_statement, compute_totals

ROOT = Path(__file__).resolve().parents[1]
STATEMENT = SBER_DIR / "statement.pdf"


@pytest.fixture(scope="module")
def rules() -> Rules:
    return Rules.load(RULES)


@pytest.fixture(scope="module")
def importer(rules) -> Importer:
    return Importer(SBER_ACCOUNT, "RUB", SBER_NUMBER, rules)


@pytest.fixture(scope="module")
def transactions(importer) -> list[data.Transaction]:
    return importer.extract(str(STATEMENT), [])


def by_text(transactions, text: str) -> data.Transaction:
    found = [
        t
        for t in transactions
        if text in (t.narration or "") or text in (t.meta.get("details") or "")
    ]
    assert len(found) == 1, f"ожидалась одна проводка с {text!r}, нашлось {len(found)}"
    return found[0]


def test_golden_file_matches():
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "sber_test.py"), "test", str(SBER_DIR)],
        cwd=ROOT,
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ───────────────────────────── опознание файла ─────────────────────────────


def test_identifies_own_statement(importer):
    assert importer.identify(str(STATEMENT))
    assert importer.date(str(STATEMENT)).isoformat() == "2026-03-31"
    assert importer.filename(str(STATEMENT)) == "sber-rub.pdf"


def test_account_number_ignores_the_spaces_the_bank_prints(rules):
    """В выписке номер напечатан группами: «10000 000 0 0000 0000003»."""
    stranger = Importer("Assets:Sber:Other", "RUB", SBER_NUMBER_OTHER, rules)
    assert not stranger.identify(str(STATEMENT))
    # А с тем же номером, но без пробелов — опознаётся.
    assert Importer("Assets:Sber:Rub", "RUB", SBER_NUMBER, rules).identify(str(STATEMENT))


def test_tbank_statement_is_not_taken(importer):
    """Две выписки в PDF из разных банков не должны путаться."""
    assert not importer.identify(str(ROOT / "tests" / "tbank" / "statement.pdf"))


def test_broken_file_is_not_identified(importer, tmp_path):
    garbage = tmp_path / "garbage.pdf"
    garbage.write_bytes(b"%PDF-1.5 and then nothing that makes sense")
    assert not importer.identify(str(garbage))

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert not importer.identify(str(empty))

    assert not importer.identify(str(ROOT / "tests" / "acba" / "card" / "card.xls"))


def test_names_are_distinct(rules):
    first = Importer("Assets:Sber:Rub", "RUB", SBER_NUMBER, rules)
    second = Importer("Assets:Sber:Savings", "RUB", SBER_NUMBER_OTHER, rules)
    assert first.name != second.name


# ─────────────────────────────── разбор строк ───────────────────────────────


def test_every_row_of_the_fixture_is_extracted(transactions):
    """Повторная шапка таблицы и подвал не должны сойти за операции."""
    assert len(transactions) == len(ROWS)


def test_sign_comes_from_the_plus_not_from_the_minus(transactions):
    """У Сбербанка расход печатается БЕЗ знака, а приход — с плюсом.

    Строка `1 200,00` — это списание, и разобрать её как положительную значило
    бы перевернуть половину выписки.
    """
    assert transactions[0].postings[0].units.number == Decimal("-1200.00")
    assert by_text(transactions, "Заработная плата").postings[0].units.number > 0


def test_non_breaking_space_does_not_get_into_the_number(transactions):
    """Тысячи банк разделяет неразрывным пробелом, а не обычным."""
    salary = by_text(transactions, "Заработная плата")
    assert salary.postings[0].units.number == Decimal("75000.00")


def test_processing_date_only_when_it_differs(transactions):
    assert by_text(transactions, "DELIVERY ONE").meta["settlement"] == "2026-01-07"
    assert "settlement" not in transactions[0].meta


def test_bank_category_is_kept_and_offered_to_rules(transactions):
    """Категорию проставляет сам банк — она уходит и в мету, и в правила."""
    assert transactions[0].meta["bank-type"] == "Прочие расходы"
    refund = by_text(transactions, "Возврат за покупку")
    assert refund.meta["bank-type"] == "Возврат, отмена операции"
    # Правило sber-refund матчит именно по `type`.
    assert refund.postings[1].account == "Expenses:Food:Groceries"


def test_card_number_is_split_off_the_description(transactions):
    """Хвост «Операция по карте ****0001» повторяется в каждой строке."""
    assert transactions[0].meta["card"] == "****0001"
    assert "Операция по" not in transactions[0].narration
    # «Операция по счету» — это номер того же счёта, в мете ему делать нечего.
    assert "card" not in by_text(transactions, "Заработная плата").meta


def test_uncategorised_rows_are_flagged(transactions):
    for txn in transactions:
        uncategorised = txn.postings[-1].account == "Expenses:Uncategorized"
        assert (txn.flag == "!") == uncategorised


def test_no_balance_directive_is_emitted(importer):
    """Остатка по счёту в выписке Сбербанка нет ни в каком виде."""
    entries = importer.extract(str(STATEMENT), [])
    assert not [e for e in entries if isinstance(e, data.Balance)]


# ──────────────────────── комиссия внутри суммы ────────────────────────


def test_fee_is_carved_out_of_the_amount(transactions):
    """«В сумму операции ВКЛЮЧЕНА комиссия» — её ногу надо вычесть, а не добавить.

    У ACBA наоборот: там комиссия к сумме операции прибавляется.
    """
    transfer = by_text(transactions, "Перевод для И. Пётр Сергеевич")
    assert len(transfer.postings) == 3
    assert transfer.postings[0].units.number == Decimal("-10037.50")
    assert transfer.postings[1].account == "Expenses:Fees:Bank:Transfer"
    assert transfer.postings[1].units.number == Decimal("37.50")
    # Получателю ушло ровно 10 000: остаток выводит beancount.
    assert transfer.postings[2].units is None


def test_rows_without_a_fee_have_two_postings(transactions):
    assert len(transactions[0].postings) == 2


def test_fee_does_not_shift_the_category(rules, tmp_path):
    """Правило подбирается по сумме операции без комиссии."""
    row = Row("05.01.2026", "09:12", "05.01.2026", "100001", "Прочие расходы",
              "1" + chr(0x00A0) + "237,50",
              "SUPERMARKET ONE MOSCOW RUS. Операция по карте ****0001", fee="37,50")
    path = build_statement(tmp_path / "fee.pdf", rows=(row,))
    txn = Importer(SBER_ACCOUNT, "RUB", SBER_NUMBER, rules).extract(str(path), [])[0]
    assert txn.postings[0].units.number == Decimal("-1237.50")
    assert txn.postings[1].units.number == Decimal("37.50")
    assert txn.postings[2].account == "Expenses:Food:Groceries"


# ──────────────────────────────── дедупликация ────────────────────────────────


def test_operation_key_is_the_authorisation_code(transactions):
    keys = [t.meta["sber-id"] for t in transactions]
    assert len(set(keys)) == len(keys)
    assert keys[0].endswith(":100001-1")


def test_only_the_overlapping_part_is_marked(importer, transactions, tmp_path):
    """Выписки внахлёст: повторяются не все операции, а часть."""
    overlap = (ROWS[0], ROWS[2])
    short = build_statement(tmp_path / "short.pdf", rows=overlap)
    earlier = importer.extract(str(short), [])

    again = importer.extract(str(STATEMENT), earlier)
    importer.deduplicate(again, earlier)
    marked = [e for e in again if extract.DUPLICATE in e.meta]
    assert len(marked) == len(overlap)


# ──────────────────────────── сверка с итогами ────────────────────────────


def test_totals_that_disagree_with_the_table_stop_the_import(rules, tmp_path):
    """Потерянная разбором строка обязана обрушить импорт, а не проехать молча."""
    rows = (*ROWS[:2], Row("07.01.2026", "10:00", "07.01.2026", "100099",
                           "Прочие расходы", "1,00", "MISSED ONE. Операция по карте ****0001"))
    path = build_statement(tmp_path / "wrong.pdf", rows=rows, totals=compute_totals(rows[:-1]))
    with pytest.raises(ValueError, match="Похоже, разбор потерял строки"):
        Importer(SBER_ACCOUNT, "RUB", SBER_NUMBER, rules).extract(str(path), [])


def test_statement_without_totals_is_rejected(rules, tmp_path):
    path = build_statement(tmp_path / "no-totals.pdf", rows=ROWS[:2], totals=None)
    with pytest.raises(ValueError, match="не найден блок"):
        Importer(SBER_ACCOUNT, "RUB", SBER_NUMBER, rules).extract(str(path), [])


# ───────────────────────────── защита от ошибок ─────────────────────────────


def test_currency_mismatch_with_the_account_is_rejected(rules):
    """Выписка в рублях не должна молча записаться на долларовый счёт."""
    importer = Importer("Assets:Sber:Usd", "USD", SBER_NUMBER, rules)
    with pytest.raises(ValueError, match="не совпадает с валютой счёта"):
        importer.extract(str(STATEMENT), [])


def test_account_does_not_open_the_file(importer, tmp_path):
    """account() обязан отвечать не заглядывая в файл: так его зовёт beangulp."""
    assert importer.account(str(tmp_path / "нет-такого.pdf")) == SBER_ACCOUNT


def test_fixture_font_covers_every_character():
    from tools.make_sber_fixture import needed_characters
    from tools.make_tbank_fixture import register_font

    assert register_font(needed_characters()).exists()


def test_fixture_header_stays_in_placeholders():
    """Номер счёта фикстуры — заглушка: настоящий префикс 40817 сюда нельзя."""
    assert Header().account.replace(" ", "").startswith("1000")
