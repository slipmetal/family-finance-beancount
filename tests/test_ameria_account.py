"""Тесты импортёра выписок по счетам Ameriabank (.csv).

Формат отличается от карточного целиком — см. докстринг finance/importers/ameria.py.
"""

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
from finance.importers.ameria import AccountImporter, CardImporter
from tests.fixtures import (
    AMERIA_ACCOUNT_DIR,
    AMERIA_CARD_DIR,
    AMERIA_MARKER,
    AMERIA_SAVINGS_ACCOUNT,
    AMERIA_SAVINGS_MARKER,
    AMERIA_SAVINGS_NUMBER,
    RULES,
)

ROOT = Path(__file__).resolve().parents[1]
STATEMENT = AMERIA_ACCOUNT_DIR / "usd_statement.csv"
CARD_STATEMENT = AMERIA_CARD_DIR / "card0001_statement.csv"


@pytest.fixture(scope="module")
def rules() -> Rules:
    return Rules.load(RULES)


@pytest.fixture(scope="module")
def importer(rules) -> AccountImporter:
    return AccountImporter(
        AMERIA_SAVINGS_ACCOUNT, "USD", rules,
        marker=AMERIA_SAVINGS_MARKER, number=AMERIA_SAVINGS_NUMBER,
    )


@pytest.fixture(scope="module")
def transactions(importer) -> list[data.Transaction]:
    return importer.extract(str(STATEMENT), [])


def by_type(transactions, kind: str) -> list[data.Transaction]:
    return [t for t in transactions if t.meta.get("bank-type") == kind]


def test_golden_file_matches():
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "ameria_account_test.py"), "test", str(AMERIA_ACCOUNT_DIR)],
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
    assert importer.filename(str(STATEMENT)) == "ameria-usd-account.csv"


def test_two_formats_do_not_take_each_other(rules):
    """Карточная выписка и выписка по счёту — разные CSV одного банка."""
    card = CardImporter("Assets:Ameria:Card0001", "AMD", rules, marker=AMERIA_MARKER)
    account = AccountImporter(
        AMERIA_SAVINGS_ACCOUNT, "USD", rules,
        marker=AMERIA_SAVINGS_MARKER, number=AMERIA_SAVINGS_NUMBER,
    )
    assert card.identify(str(CARD_STATEMENT))
    assert not account.identify(str(CARD_STATEMENT))
    assert account.identify(str(STATEMENT))
    assert not card.identify(str(STATEMENT))


def test_marker_in_filename_selects_the_account(rules, tmp_path):
    """Номера счёта в файле нет, поэтому счёт определяется именем файла."""
    raw = STATEMENT.read_text(encoding="utf-8")
    mine = tmp_path / "usd_2026-08.csv"
    mine.write_text(raw, encoding="utf-8")
    plain = tmp_path / "export_0000000.csv"
    plain.write_text(raw, encoding="utf-8")

    imp = AccountImporter(
        AMERIA_SAVINGS_ACCOUNT, "USD", rules,
        marker=AMERIA_SAVINGS_MARKER, number=AMERIA_SAVINGS_NUMBER,
    )
    assert imp.identify(str(mine))
    # Без метки файл не достаётся никому — лучше, чем достаться не тому.
    assert not imp.identify(str(plain))


def test_account_number_vetoes_a_mislabelled_file(rules):
    """В описании процентов банк печатает хвост номера — он и перепроверяет метку."""
    stranger = AccountImporter(
        "Assets:Ameria:Other", "USD", rules,
        marker=AMERIA_SAVINGS_MARKER, number="1000000000000000",
    )
    assert not stranger.identify(str(STATEMENT))


def test_veto_is_not_a_requirement(rules, tmp_path):
    """У выписки без начисления процентов номера счёта нет вообще.

    Такой файл должен опознаваться по метке: иначе выписка за месяц без
    процентов просто не импортировалась бы.
    """
    lines = STATEMENT.read_text(encoding="utf-8").splitlines()
    without = [lines[0]] + [x for x in lines[1:] if "ըստ" not in x]
    path = tmp_path / "usd_no_interest.csv"
    path.write_text("\n".join(without) + "\n", encoding="utf-8")

    imp = AccountImporter(
        AMERIA_SAVINGS_ACCOUNT, "USD", rules,
        marker=AMERIA_SAVINGS_MARKER, number="1000000000000000",
    )
    assert imp.identify(str(path))


def test_number_in_the_file_identifies_it_without_a_marker(rules, tmp_path):
    """Хвост номера из описания процентов заменяет метку, если счёт по нему
    отличается от остальных. Такую выписку переименовывать не нужно."""
    raw = STATEMENT.read_text(encoding="utf-8")
    plain = tmp_path / "export_00000000000000000001.csv"
    plain.write_text(raw, encoding="utf-8")

    mine = AccountImporter(
        AMERIA_SAVINGS_ACCOUNT, "USD", rules,
        marker=AMERIA_SAVINGS_MARKER, number=AMERIA_SAVINGS_NUMBER, marker_optional=True,
    )
    assert mine.identify(str(plain))

    stranger = AccountImporter(
        "Assets:Ameria:Other", "USD", rules,
        marker="other", number="1000000000000000", marker_optional=True,
    )
    assert not stranger.identify(str(plain))


def test_statement_without_interest_still_needs_a_marker(rules, tmp_path):
    """Без процентов о счёте в файле нет ничего — валюты в этом формате тоже.

    С меткой такой файл опознаётся (см. test_veto_is_not_a_requirement),
    без метки — не должен: подтвердить его нечем.
    """
    lines = STATEMENT.read_text(encoding="utf-8").splitlines()
    without = "\n".join([lines[0]] + [x for x in lines[1:] if "ըստ" not in x]) + "\n"
    named = tmp_path / "usd_no_interest.csv"
    named.write_text(without, encoding="utf-8")
    plain = tmp_path / "export_00000000000000000002.csv"
    plain.write_text(without, encoding="utf-8")

    imp = AccountImporter(
        AMERIA_SAVINGS_ACCOUNT, "USD", rules,
        marker=AMERIA_SAVINGS_MARKER, number=AMERIA_SAVINGS_NUMBER, marker_optional=True,
    )
    assert imp.identify(str(named))
    assert not imp.identify(str(plain))


def test_broken_file_is_not_identified(importer, tmp_path):
    garbage = tmp_path / "usd_garbage.csv"
    garbage.write_text("не то и не в том формате\n", encoding="utf-8")
    assert not importer.identify(str(garbage))
    assert not importer.identify(str(ROOT / "tests" / "acba" / "card" / "card.xls"))


def test_names_are_distinct(rules):
    """Метку задаёт человек, и она может совпасть у карты и у счёта."""
    card = CardImporter("Assets:Ameria:Rub", "RUB", rules, marker="usd")
    account = AccountImporter(
        AMERIA_SAVINGS_ACCOUNT, "USD", rules, marker="usd", number=AMERIA_SAVINGS_NUMBER
    )
    assert card.name != account.name


# ─────────────────────────────── разбор строк ───────────────────────────────


def test_totals_row_is_not_an_operation(transactions):
    """Последняя строка выписки — обороты, а не операция."""
    assert len(transactions) == 9
    assert all(t.date.year == 2026 for t in transactions)


def test_credit_and_debit_become_one_signed_amount(transactions):
    """Колонки раздельные и без знака: приход в Credit, расход в Debit."""
    credit = by_type(transactions, "Between my accounts")[0]
    assert credit.postings[0].units.number == Decimal("500.0")
    debit = by_type(transactions, "Between my accounts")[1]
    assert debit.postings[0].units.number == Decimal("-60.0")


def test_correspondent_reaches_the_rules(transactions):
    """Номер счёта контрагента есть только в этом формате."""
    first = transactions[0]
    assert first.meta["correspondent"] == "1000012345678901"
    assert all("correspondent" in t.meta for t in transactions)


def test_bank_type_is_kept_and_offered_to_rules(transactions):
    """Тип операции здесь осмысленный, в отличие от карточной выписки."""
    interest = by_type(transactions, "Interest repayment")
    assert len(interest) == 2
    # Правило ameria-savings-interest матчит именно по `type`.
    assert all(t.postings[-1].account == "Income:Interest" for t in interest)
    assert all(t.postings[-1].account == "Expenses:Taxes:Income"
               for t in by_type(transactions, "Tax charge"))


def test_deposit_goes_to_its_own_account(transactions):
    deposit = by_type(transactions, "Deposit replenishment")[0]
    assert deposit.postings[0].units.number == Decimal("-1000.0")
    assert deposit.postings[-1].account == "Assets:Ameria:Deposit"


def test_every_transaction_has_at_least_two_postings(transactions):
    for txn in transactions:
        assert len(txn.postings) >= 2
        assert txn.postings[-1].units is None


# ──────────────────────────────── дедупликация ────────────────────────────────


def test_document_number_is_made_unique(transactions):
    """У процентов и удержанного с них налога номер документа ОБЩИЙ."""
    keys = [t.meta["ameria-id"] for t in transactions]
    assert len(set(keys)) == len(keys)
    shared = [k for k in keys if ":100002-" in k]
    assert sorted(shared) == [
        "usd:2026-01-31:100002-1",
        "usd:2026-01-31:100002-2",
    ]


def test_row_without_a_document_number_still_gets_a_key(transactions):
    """У части операций номера документа нет вовсе."""
    deposit = by_type(transactions, "Deposit replenishment")[0]
    assert deposit.meta["ameria-id"] == "usd:2026-03-15:-1"


def test_key_carries_the_date(transactions):
    """Номера документов короткие и сквозные внутри периода — в следующем
    году они пойдут по второму кругу, поэтому дата в ключе обязательна."""
    short = [t for t in transactions if t.meta["ameria-id"].endswith(":172-1")]
    assert short and short[0].meta["ameria-id"].startswith("usd:2026-02-14:")


def test_reimport_marks_everything_as_duplicate(importer, transactions):
    again = importer.extract(str(STATEMENT), transactions)
    importer.deduplicate(again, transactions)
    marked = [e for e in again if extract.DUPLICATE in e.meta]
    assert len(marked) == len(transactions)


# ──────────────────────────── сверка с оборотами ────────────────────────────


def test_totals_that_disagree_stop_the_import(importer, tmp_path):
    """Потерянная строка обязана обрушить импорт, а не проехать молча."""
    lines = STATEMENT.read_text(encoding="utf-8").splitlines()
    # Выкидываем одну операцию, строку оборотов оставляем прежней.
    broken = [lines[0]] + lines[2:]
    path = tmp_path / "usd_broken.csv"
    path.write_text("\n".join(broken) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Похоже, разбор потерял строки"):
        importer.extract(str(path), [])


def test_statement_without_totals_is_rejected(importer, tmp_path):
    lines = STATEMENT.read_text(encoding="utf-8").splitlines()
    path = tmp_path / "usd_no_totals.csv"
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="нет строки оборотов"):
        importer.extract(str(path), [])