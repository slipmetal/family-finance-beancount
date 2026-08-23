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
from finance.config import ConfigError, build_importers, load_accounts
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


def test_wrong_shape_is_rejected(tmp_path):
    path = tmp_path / "accounts.yaml"
    path.write_text("- Assets:X:Y\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ожидался ключ `accounts`"):
        load_accounts(path)