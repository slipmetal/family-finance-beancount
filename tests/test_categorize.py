"""Тесты движка правил — отдельно от CSV и от конкретных банков."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from beancount import loader
from beancount.core import data

from finance.categorize import Rules, RulesError

ROOT = Path(__file__).resolve().parents[1]


def make_rules(tmp_path: Path, config: dict) -> Rules:
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return Rules.load(path)


def apply(rules: Rules, *, counterparty="", details="", txn_type="", amount="-100", account=""):
    return rules.apply(
        counterparty=counterparty,
        details=details,
        txn_type=txn_type,
        amount=Decimal(amount),
        account=account,
    )


# ───────────────────────────── подбор правила ─────────────────────────────


def test_first_matching_rule_wins(tmp_path):
    """Порядок в файле значим: частные правила ставятся выше общих."""
    rules = make_rules(
        tmp_path,
        {
            "rules": [
                {"name": "delivery", "match": {"details": "SAS DELIVERY"},
                 "account": "Expenses:Food:Delivery"},
                {"name": "shop", "match": {"details": "SAS"},
                 "account": "Expenses:Food:Groceries"},
            ]
        },
    )
    assert apply(rules, details="Ք: SAS DELIVERY 15").account == "Expenses:Food:Delivery"
    assert apply(rules, details="Ք: SAS YEREVAN").account == "Expenses:Food:Groceries"


def test_match_keys_are_combined_with_and(tmp_path):
    """Условия внутри `match` складываются по И: одного мало."""
    rules = make_rules(
        tmp_path,
        {
            "rules": [
                {
                    "name": "rent",
                    "match": {"details": "^Personal transfer$", "counterparty": "IVANOV"},
                    "account": "Expenses:Home:Rent",
                }
            ]
        },
    )
    assert apply(rules, details="Personal transfer", counterparty="IVANOV").rule == "rent"
    # Каждого условия по отдельности недостаточно.
    assert not apply(rules, details="Personal transfer", counterparty="PETROV").matched
    assert not apply(rules, details="Transfer", counterparty="IVANOV").matched


def test_sign_separates_income_from_expense(tmp_path):
    """Приход и расход с одинаковым описанием — разные счета."""
    rules = make_rules(
        tmp_path,
        {
            "rules": [
                {"name": "fee", "match": {"details": "^Сбор$", "sign": "-"},
                 "account": "Expenses:Fees:Bank:Service"},
                {"name": "topup", "match": {"details": "^Сбор$", "sign": "+"},
                 "account": "Assets:Transfers:Pending"},
            ]
        },
    )
    assert apply(rules, details="Сбор", amount="-2500").account == "Expenses:Fees:Bank:Service"
    assert apply(rules, details="Сбор", amount="2500").account == "Assets:Transfers:Pending"
    # Ноль не относится ни к приходу, ни к расходу.
    assert not apply(rules, details="Сбор", amount="0").matched


def test_amount_thresholds_compare_magnitude(tmp_path):
    """Пороги заданы по модулю, чтобы не дублировать их для прихода и расхода."""
    rules = make_rules(
        tmp_path,
        {
            "rules": [
                {"name": "small", "match": {"details": "x", "amount_max": 100},
                 "account": "Expenses:Misc"}
            ]
        },
    )
    assert apply(rules, details="x", amount="-50").matched
    assert apply(rules, details="x", amount="50").matched
    assert apply(rules, details="x", amount="-100").matched, "граница включительно"
    assert not apply(rules, details="x", amount="-101").matched


def test_unmatched_falls_back_to_default(tmp_path):
    """Не подошло ни одно правило — счёт по умолчанию и пустой результат."""
    rules = make_rules(
        tmp_path,
        {
            "default_account": "Expenses:Uncategorized",
            "rules": [{"name": "r", "match": {"details": "zzz"}, "account": "Expenses:Misc"}],
        },
    )
    result = apply(rules, details="что-то новое")
    assert result.account == "Expenses:Uncategorized"
    assert result.rule is None
    assert not result.matched
    assert result.payee is None and result.tags == frozenset()


def test_rule_carries_payee_narration_and_tags(tmp_path):
    """Правило задаёт не только счёт: контрагент, описание и теги едут с ним."""
    rules = make_rules(
        tmp_path,
        {
            "rules": [
                {
                    "name": "own",
                    "match": {"details": "own funds"},
                    "account": "Assets:Transfers:Pending",
                    "payee": "Я сам",
                    "narration": "Перевод между своими счетами",
                    "tags": ["transit"],
                }
            ]
        },
    )
    result = apply(rules, details="Transfer of own funds")
    assert result.payee == "Я сам"
    assert result.narration == "Перевод между своими счетами"
    assert result.tags == frozenset({"transit"})


# ─────────────────── правило, привязанное к счёту выписки ───────────────────


def two_accounts(tmp_path) -> Rules:
    """Одно описание, два счёта, разный смысл.

    Ровно тот случай, ради которого поле и появилось: «Пополнение из Сбербанка»
    на счёте, чья вторая сторона в леджере есть, — транзит, а на любом другом
    счёте той же семьи — деньги извне.
    """
    return make_rules(
        tmp_path,
        {
            "rules": [
                {
                    "name": "topup-known",
                    "match": {"details": "^Пополнение$", "account": "^Assets:Bank:Mine$"},
                    "account": "Assets:Transfers:Pending",
                    "tags": ["transit"],
                },
                {
                    "name": "topup-any",
                    "match": {"details": "^Пополнение$"},
                    "account": "Assets:External:Somewhere",
                },
            ]
        },
    )


def test_account_narrows_the_rule_to_one_statement(tmp_path):
    rules = two_accounts(tmp_path)
    mine = apply(rules, details="Пополнение", account="Assets:Bank:Mine")
    other = apply(rules, details="Пополнение", account="Assets:Bank:Other")
    assert mine.rule == "topup-known"
    assert mine.account == "Assets:Transfers:Pending"
    assert other.rule == "topup-any"
    assert other.account == "Assets:External:Somewhere"


def test_account_unset_falls_through_to_the_general_rule(tmp_path):
    """Импортёр, который счёт не передал, не должен случайно попасть в узкое правило."""
    rules = two_accounts(tmp_path)
    assert apply(rules, details="Пополнение").rule == "topup-any"


def test_account_is_not_part_of_text(tmp_path):
    """Иначе правило на мерчанта цеплялось бы за имя счёта."""
    rules = make_rules(
        tmp_path,
        {"rules": [
            {"name": "merchant", "match": {"text": "Tbank"}, "account": "Expenses:Shopping"}
        ]},
    )
    assert not apply(rules, details="Оплата в магазине", account="Assets:Tbank:Rub").matched


def test_account_is_a_regexp_like_the_other_fields(tmp_path):
    """Префикс покрывает все счета банка разом."""
    rules = make_rules(
        tmp_path,
        {"rules": [{"name": "any-tbank", "match": {"account": "^Assets:Tbank:"},
                    "account": "Expenses:Shopping"}]},
    )
    assert apply(rules, account="Assets:Tbank:Rub").matched
    assert apply(rules, account="Assets:Tbank:Savings").matched
    assert not apply(rules, account="Assets:Sber:Rub").matched


def test_categorize_takes_the_account_from_the_first_posting(tmp_path):
    """Стык с booking: счёт правилам передаёт не импортёр, а сама заготовка.

    Ломается незаметно — если импортёр поставит ногу банка не первой, правило
    на счёт молча перестанет срабатывать, — поэтому проверяется отдельно.
    """
    import datetime as dt

    from beancount.core import amount as bc_amount

    from finance.booking import categorize

    rules = two_accounts(tmp_path)
    txn = data.Transaction(
        {}, dt.date(2026, 1, 1), "*", None, "", frozenset(), frozenset(),
        [data.Posting("Assets:Bank:Mine", bc_amount.Amount(Decimal("100.00"), "RUB"),
                      None, None, None, None)],
    )
    built = categorize(txn, rules, counterparty="", details="Пополнение",
                       txn_type="", amount=Decimal("100.00"))
    assert built.postings[-1].account == "Assets:Transfers:Pending"
    assert "transit" in built.tags


# ──────────────────────── ошибки находятся при загрузке ────────────────────────


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        pytest.param(
            {"rules": [{"name": "r", "match": {"details": "["}, "account": "Expenses:Misc"}]},
            "невалидная регулярка",
            id="сломанная регулярка",
        ),
        pytest.param(
            {"rules": [{"name": "r", "match": {"merchant": "x"}, "account": "Expenses:Misc"}]},
            "неизвестные ключи в `match`",
            id="опечатка в ключе match",
        ),
        pytest.param(
            {"rules": [{"name": "r", "match": {"details": "x"}, "account": "Продукты"}]},
            "не похоже на имя счёта",
            id="счёт не по правилам beancount",
        ),
        pytest.param(
            {"rules": [{"name": "r", "match": {"details": "x"}}]},
            "обязателен `account`",
            id="забыли счёт",
        ),
        pytest.param(
            {"rules": [{"name": "r", "account": "Expenses:Misc"}]},
            "обязателен непустой `match`",
            id="правило без условий",
        ),
        pytest.param(
            {
                "rules": [
                    {"name": "dup", "match": {"details": "a"}, "account": "Expenses:Misc"},
                    {"name": "dup", "match": {"details": "b"}, "account": "Expenses:Misc"},
                ]
            },
            "уже занято",
            id="одинаковые имена правил",
        ),
        pytest.param(
            {
                "rules": [
                    {"name": "r", "match": {"details": "x"}, "account": "Expenses:Misc",
                     "tags": ["две слова"]}
                ]
            },
            "недопустимый тег",
            id="тег с пробелом",
        ),
        pytest.param(
            {"rules": [{"name": "r", "match": {"details": "x", "sign": "*"},
                        "account": "Expenses:Misc"}]},
            re.escape("`match.sign` должен быть '+' или '-'"),
            id="непонятный знак",
        ),
    ],
)
def test_broken_config_fails_on_load(tmp_path, config, expected):
    """Каждая ошибка из списка выше обязана всплыть при загрузке правил."""
    with pytest.raises(RulesError, match=expected):
        make_rules(tmp_path, config)


def test_empty_file_is_rejected(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(RulesError, match="пуст"):
        Rules.load(path)


# ───────────────────────── порядок правил на живом примере ─────────────────────
# Имена и номера здесь выдуманные: боевые правила лежат рядом с леджером,
# в приватном репозитории, и в тестах не участвуют.


def test_landlord_transfers_are_split_by_amount(tmp_path):
    """Аренда и коммуналка идут одному человеку, различаются только суммой.

    Заодно проверяется порядок правил: у переводов со счёта ACBA описание
    «Transfer to account», и общее правило переводов увело бы аренду в транзит,
    если бы стояло выше. Это ровно та расстановка, что нужна в боевом файле.
    """
    rules = make_rules(
        tmp_path,
        {
            "default_account": "Expenses:Uncategorized",
            "rules": [
                {
                    "name": "rent",
                    "match": {
                        "text": "(?i)ivanov|Իվանով",
                        "amount_min": 300000,
                        "amount_max": 300000,
                    },
                    "account": "Expenses:Home:Rent",
                },
                {
                    "name": "utilities-to-landlord",
                    "match": {"text": "(?i)ivanov|Իվանով"},
                    "account": "Expenses:Home:Utilities",
                },
                {
                    "name": "transfer-own-account",
                    "match": {"details": "^Transfer to account"},
                    "account": "Assets:Transfers:Pending",
                    "tags": ["transit"],
                },
            ],
        },
    )

    def apply(details, amount, counterparty="", txn_type="TRF"):
        return rules.apply(
            counterparty=counterparty,
            details=details,
            txn_type=txn_type,
            amount=Decimal(amount),
        )

    transfer = "Transfer to account (մոբայլ բանկինգ 100000000)"
    assert apply(transfer, "-300000", "Իվանով Իվան").account == "Expenses:Home:Rent"
    assert apply(transfer, "-53000", "Իվանով Իվան").account == "Expenses:Home:Utilities"
    # Второй счёт того же человека, имя латиницей.
    assert apply(transfer, "-300000", "IVAN IVANOV").account == "Expenses:Home:Rent"

    # Карточный перевод: имя приходит внутри описания, а не отдельным полем.
    card = "Card to card transfer, [IVANOV IVAN], FIRSTNAME LASTNAME, 5555********0001"
    assert apply(card, "-300000", txn_type="card").account == "Expenses:Home:Rent"
    assert apply(card, "-60000", txn_type="card").account == "Expenses:Home:Utilities"

    # Однофамилец по имени, но не по фамилии — под правило попасть не должен.
    other = "Card to card transfer, IVAN PETROV, FIRSTNAME LASTNAME, 5555********0002"
    assert not apply(other, "-300000", txn_type="card").matched


# ──────────────────────── файлы правил в репозитории ────────────────────────


@pytest.mark.parametrize("name", ["rules.example.yaml", "tests/rules.yaml"])
def test_shipped_rules_load(name):
    """Оба файла правил в репозитории обязаны быть валидными.

    Образец — потому что с него начинают, тестовый — потому что на нём держатся
    эталонные файлы.
    """
    rules = Rules.load(ROOT / name)
    assert rules.rules, "правила не должны быть пустыми"


def test_every_rule_account_is_open_in_ledger():
    """Счёт из правила, не заведённый в плане счетов, обрушит проверку леджера.

    Ловим это тестом, а не при первом импорте. Леджер и боевые правила лежат
    в отдельном приватном репозитории: нет клона — нечего и проверять.
    """
    ledger = ROOT / "ledger" / "main.beancount"
    rules_path = ROOT / "ledger" / "rules.yaml"
    if not ledger.exists() or not rules_path.exists():
        pytest.skip("леджер не склонирован в ledger/ — проверять нечего")

    entries, errors, _ = loader.load_file(str(ledger))
    assert not errors, [e.message for e in errors]

    opened = {e.account for e in entries if isinstance(e, data.Open)}
    rules = Rules.load(rules_path)
    used = {rule.account for rule in rules.rules} | {rules.default_account}

    assert used <= opened, f"не заведены в ledger/accounts.beancount: {sorted(used - opened)}"
