"""Тесты импортёров ACBA — карточного (.xls) и обычного счёта (.xml)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from beancount.core import data

from finance.categorize import Rules
from finance.importers.acba import AccountImporter, CardImporter
from tests.conftest import check_golden
from tests.fixtures import (
    ACBA_ACCOUNT_DIR,
    ACBA_ACCOUNT_NUMBER,
    ACBA_ACCOUNT_NUMBER_RUB,
    ACBA_ACCOUNT_NUMBER_USD,
    ACBA_CARD_DIR,
    ACBA_CARD_NUMBER,
    ACBA_CARD_NUMBER_USD,
    RULES,
)

CARD_DIR = ACBA_CARD_DIR
ACCOUNT_DIR = ACBA_ACCOUNT_DIR
CARD = CARD_DIR / "card.xls"
ACCOUNT = ACCOUNT_DIR / "account.xml"

CARD_NUMBER = ACBA_CARD_NUMBER
ACCOUNT_NUMBER = ACBA_ACCOUNT_NUMBER


@pytest.fixture(scope="module")
def rules() -> Rules:
    return Rules.load(RULES)


@pytest.fixture(scope="module")
def card(rules) -> CardImporter:
    return CardImporter("Assets:Acba:AmdCard", "AMD", CARD_NUMBER, rules)


@pytest.fixture(scope="module")
def account(rules) -> AccountImporter:
    return AccountImporter("Assets:Acba:Amd", "AMD", ACCOUNT_NUMBER, rules)


def test_card_golden_file_matches():
    check_golden("acba-card")


def test_account_golden_file_matches():
    check_golden("acba-account")


# ───────────────────────────── опознание файла ─────────────────────────────


def test_each_importer_takes_only_its_own_format(card, account):
    """Карты читаются из xls, счета из xml — перепутать нельзя."""
    assert card.identify(str(CARD))
    assert not card.identify(str(ACCOUNT))
    assert account.identify(str(ACCOUNT))
    assert not account.identify(str(CARD))


def test_account_number_from_file_selects_the_importer(rules):
    """Номер счёта лежит в самом файле, поэтому папки для ACBA не нужны."""
    stranger = CardImporter("Assets:Acba:UsdCard", "USD", ACBA_CARD_NUMBER_USD, rules)
    assert not stranger.identify(str(CARD))

    stranger = AccountImporter("Assets:Acba:Rub", "RUB", ACBA_ACCOUNT_NUMBER_RUB, rules)
    assert not stranger.identify(str(ACCOUNT))


def test_broken_file_is_not_identified(card, account, tmp_path):
    junk_xls = tmp_path / "junk.xls"
    junk_xls.write_bytes(b"not an excel file at all")
    assert not card.identify(str(junk_xls))

    junk_xml = tmp_path / "junk.xml"
    junk_xml.write_text("<nonsense/>", encoding="utf-8")
    assert not account.identify(str(junk_xml))


# ───────────────────────────── карта: разбор ─────────────────────────────


def test_card_skips_daily_balance_rows(card):
    """Между операциями банк вставляет строку с остатком на конец дня."""
    entries = card.extract(str(CARD), [])
    txns = [e for e in entries if isinstance(e, data.Transaction)]
    # В фикстуре 11 операций и одна строка остатка — она не должна стать проводкой.
    assert len(txns) == 11
    for txn in txns:
        assert "Balance-" not in txn.narration


def test_card_emits_final_balance_assertion(card):
    """Итог из шапки выписки — сверка нашего разбора с расчётом банка."""
    entries = card.extract(str(CARD), [])
    balances = [e for e in entries if isinstance(e, data.Balance)]
    assert len(balances) == 1
    assert balances[0].account == "Assets:Acba:AmdCard"
    assert balances[0].amount.currency == "AMD"
    # Выписка за 01.01–21.08.2026, проверка остатка на начало следующего дня.
    assert balances[0].date.isoformat() == "2026-08-22"


def test_card_credit_and_debit_become_one_signed_amount(card):
    """Приход и расход у ACBA в разных колонках — сводим к одной сумме."""
    entries = card.extract(str(CARD), [])
    amounts = {
        e.narration: e.postings[0].units.number
        for e in entries
        if isinstance(e, data.Transaction)
    }
    assert amounts["Плата за обслуживание карты"] == Decimal("-400.00")
    # Reversal — возврат мерчанта, единственная строка в колонке Credit.
    assert any(v > 0 for k, v in amounts.items() if "Reversal" in k)


def test_card_merchant_drives_categorization(card):
    """Мерчант приходит в «месте операции», а не в описании."""
    entries = card.extract(str(CARD), [])
    glovo = next(e for e in entries if getattr(e, "payee", None) == "Glovo")
    assert glovo.meta["counterparty"] == "GLOVO APP"
    assert glovo.postings[1].account == "Expenses:Food:Delivery"


# ───────────────────────────── счёт: разбор ─────────────────────────────


def test_account_transaction_id_is_made_unique(account):
    """Перевод и удержанная за него комиссия приходят с одним TRANSACTIONID."""
    entries = account.extract(str(ACCOUNT), [])
    ids = [e.meta["acba-id"] for e in entries]
    assert len(ids) == len(set(ids)), "ключ дедупа обязан быть уникальным"
    # У пары «перевод + комиссия» общий номер и разные порядковые. Номер не
    # зашит в тест: фикстура пересобирается, и номера в ней перенумеровываются.
    numbers = [i.split(":", 1)[1].rsplit("-", 1)[0] for i in ids]
    shared = next(n for n in numbers if numbers.count(n) == 2)
    assert sorted(i for i in ids if shared in i) == [
        f"{ACCOUNT_NUMBER}:{shared}-1",
        f"{ACCOUNT_NUMBER}:{shared}-2",
    ]


def test_own_transfer_legs_are_not_mistaken_for_duplicates(rules):
    """Обе ноги перевода между своими счетами приходят с одним TRANSACTIONID.

    Ключ дедупа обязан включать номер счёта, иначе нога второго счёта
    считается дубликатом первой и молча теряется при импорте.
    """
    first = AccountImporter("Assets:Acba:Amd", "AMD", ACCOUNT_NUMBER, rules)
    second = AccountImporter("Assets:Acba:Usd", "USD", ACBA_ACCOUNT_NUMBER_USD, rules)

    mine = first.extract(str(ACCOUNT), [])
    # Тот же файл глазами другого счёта: номера операций совпадают полностью.
    theirs = second.extract(str(ACCOUNT), [])
    second.deduplicate(theirs, list(mine))
    assert not any("__duplicate__" in e.meta for e in theirs)


def test_account_deduplicates_by_transaction_id(account):
    """Повторный импорт того же файла не должен давать новых проводок.

    Эвристика по дате и сумме тут не годится: по рублёвому счёту сотни
    операций обмена с одинаковыми суммами в соседние дни.
    """
    first = account.extract(str(ACCOUNT), [])
    second = account.extract(str(ACCOUNT), [])
    account.deduplicate(second, list(first))
    assert all("__duplicate__" in e.meta for e in second)


def test_account_does_not_deduplicate_unrelated_entries(account):
    first = account.extract(str(ACCOUNT), [])
    second = account.extract(str(ACCOUNT), [])
    account.deduplicate(second, [])
    assert not any("__duplicate__" in e.meta for e in second)


def test_transfer_to_own_account_goes_to_transit(account):
    """Свой счёт узнаём по номеру контрагента, а не по имени владельца."""
    entries = account.extract(str(ACCOUNT), [])
    own = [e for e in entries if e.meta.get("correspondent") == ACBA_CARD_NUMBER]
    assert own, "в фикстуре должен быть перевод на свою же карту ACBA"
    for txn in own:
        assert txn.postings[1].account == "Assets:Transfers:Pending"
        assert "transit" in txn.tags


def test_account_keeps_correspondent_and_operation_type(account):
    entries = account.extract(str(ACCOUNT), [])
    fee = next(e for e in entries if e.meta.get("bank-type") == "FEE")
    assert fee.postings[1].account == "Expenses:Fees:Bank:Transfer"
    assert fee.meta["correspondent"]


def test_currency_alias_rur_is_normalised():
    """ACBA обозначает рубль устаревшим RUR, beancount ждёт RUB."""
    from finance.importers.acba import _currency

    assert _currency("RUR") == "RUB"
    assert _currency(" amd ") == "AMD"
    assert _currency("USD") == "USD"
