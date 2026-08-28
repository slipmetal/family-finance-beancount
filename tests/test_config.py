"""Тесты списка счетов: он описан не в коде, а в accounts.yaml.

Файл личный и лежит рядом с леджером, поэтому все ошибки в нём должны
находиться при загрузке и называть номер записи — иначе опечатка всплывёт
только при импорте, а то и уведёт выписку не на тот счёт.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from finance.categorize import Rules
from finance.config import ConfigError, build_importers, load_accounts, optional_markers
from tests.fixtures import RULES

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "accounts.example.yaml"


def make_accounts(tmp_path: Path, accounts: list) -> list[dict]:
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"accounts": accounts}, allow_unicode=True), encoding="utf-8")
    return load_accounts(path)


def test_example_is_valid_and_builds_importers():
    """Образец в репозитории обязан работать: с него начинают."""
    specs = load_accounts(EXAMPLE)
    importers = build_importers(Rules.load(RULES), specs)

    assert len(importers) == len(specs)
    assert [i.account(str(EXAMPLE)) for i in importers] == [s["account"] for s in specs]
    # Имена импортёров различимы: по ним beangulp показывает, кто взял файл.
    assert len({i.name for i in importers}) == len(importers)


def test_numbers_survive_yaml_as_strings(tmp_path):
    """YAML охотно превращает номер счёта в int, а сравнивать надо строки."""
    specs = make_accounts(
        tmp_path,
        [{"bank": "acba-card", "account": "Assets:Acba:Amd", "currency": "AMD", "number": 12345678}],
    )
    assert specs[0]["number"] == "12345678"


def test_missing_file_explains_where_it_lives(tmp_path):
    with pytest.raises(ConfigError, match="accounts.example.yaml"):
        load_accounts(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    ("accounts", "expected"),
    [
        pytest.param(
            [{"bank": "sberbank", "account": "Assets:X:Y", "currency": "AMD", "number": "1"}],
            "неизвестный банк",
            id="банк без импортёра",
        ),
        pytest.param(
            [{"bank": "ameria", "account": "Assets:X:Y", "currency": "AMD"}],
            "обязательны поля",
            id="забыли метку файла",
        ),
        pytest.param(
            [{"bank": "ameria", "account": "Assets:X:Y", "marker": "x"}],
            "обязательные поля",
            id="забыли валюту",
        ),
        pytest.param(
            [{"bank": "ameria", "account": "Assets:X:Y", "currency": "AMD", "number": "1"}],
            "неизвестные поля",
            id="поле не от того банка",
        ),
        pytest.param(
            [{"bank": "ameria", "account": "Наличные", "currency": "AMD", "marker": "x"}],
            "не похоже на имя счёта",
            id="счёт не по правилам beancount",
        ),
        pytest.param(
            [
                {"bank": "ameria", "account": "Assets:X:Y", "currency": "AMD", "marker": "a"},
                {"bank": "ameria", "account": "Assets:X:Y", "currency": "AMD", "marker": "b"},
            ],
            "уже описан",
            id="один счёт дважды",
        ),
        pytest.param([], "список счетов пуст", id="пустой список"),
    ],
)
def test_broken_config_fails_on_load(tmp_path, accounts, expected):
    with pytest.raises(ConfigError, match=expected):
        make_accounts(tmp_path, accounts)


# ───────────────── кому из счетов Ameriabank нужна метка в имени ─────────────────
#
# Метку требует только тот счёт, который неотличим по содержимому выписки от
# другого счёта того же формата. Считается это по всему списку сразу — сам
# импортёр не знает, есть ли у него двойник.


def ameria(marker: str, currency: str, number: str = "") -> dict:
    """Карта, если номера нет; иначе счёт — у них разные форматы выписки."""
    account = f"Assets:Ameria:{marker.capitalize()}"
    if not number:
        return {"bank": "ameria", "account": account, "currency": currency, "marker": marker}
    return {
        "bank": "ameria-account", "account": account,
        "currency": currency, "marker": marker, "number": number,
    }


def test_card_alone_in_its_currency_does_not_need_a_marker(tmp_path):
    """Валюта есть в самой выписке: пока валюты не повторяются, метка не нужна."""
    specs = make_accounts(tmp_path, [ameria("card0001", "AMD"), ameria("rub", "RUB")])
    assert optional_markers(specs) == {"Assets:Ameria:Card0001", "Assets:Ameria:Rub"}


def test_two_cards_in_one_currency_both_keep_the_marker(tmp_path):
    """Тот самый случай, ради которого метка и появилась."""
    specs = make_accounts(
        tmp_path,
        [ameria("card0001", "AMD"), ameria("card0002", "AMD"), ameria("rub", "RUB")],
    )
    assert optional_markers(specs) == {"Assets:Ameria:Rub"}


def test_accounts_are_told_apart_by_their_numbers(tmp_path):
    """Валюты в выписке по счёту нет вообще — сравнивать остаётся номера."""
    specs = make_accounts(
        tmp_path,
        [ameria("usd", "USD", "1000053294282901"), ameria("eur", "EUR", "1000053294282902")],
    )
    assert optional_markers(specs) == {"Assets:Ameria:Usd", "Assets:Ameria:Eur"}


def test_accounts_sharing_the_last_digits_keep_their_markers(tmp_path):
    """Импортёр сверяет номер с хвостом от шести цифр: если хвосты совпали,
    выписки неразличимы, сколько бы ни отличалось начало номера."""
    specs = make_accounts(
        tmp_path,
        [ameria("usd", "USD", "1000000000282901"), ameria("eur", "EUR", "1000099999282901")],
    )
    assert optional_markers(specs) == set()


def test_currency_and_number_are_counted_separately(tmp_path):
    """Карта и счёт приходят в разных форматах, и одинаковая валюта у них
    ничего не значит: файл одного второму не достанется по одной шапке."""
    specs = make_accounts(
        tmp_path, [ameria("card0001", "USD"), ameria("usd", "USD", "1000053294282901")]
    )
    assert optional_markers(specs) == {"Assets:Ameria:Card0001", "Assets:Ameria:Usd"}


def test_flag_reaches_the_importers(tmp_path):
    """Посчитанное должно доехать до импортёра, иначе всё это впустую."""
    specs = make_accounts(
        tmp_path,
        [ameria("card0001", "AMD"), ameria("card0002", "AMD"), ameria("rub", "RUB")],
    )
    importers = build_importers(Rules.load(RULES), specs)
    assert {i.account(""): i.marker_optional for i in importers} == {
        "Assets:Ameria:Card0001": False,
        "Assets:Ameria:Card0002": False,
        "Assets:Ameria:Rub": True,
    }


def test_other_banks_never_need_a_marker(tmp_path):
    """Метка — беда одного Ameriabank: у остальных номер счёта лежит в файле."""
    specs = make_accounts(
        tmp_path,
        [
            {"bank": "acba-card", "account": "Assets:Acba:Amd", "currency": "AMD", "number": "1"},
            {"bank": "tbank", "account": "Assets:Tbank:Rub", "currency": "RUB", "number": "2"},
        ],
    )
    assert optional_markers(specs) == set()


def test_wrong_shape_is_rejected(tmp_path):
    path = tmp_path / "accounts.yaml"
    path.write_text("- Assets:X:Y\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ожидался ключ `accounts`"):
        load_accounts(path)