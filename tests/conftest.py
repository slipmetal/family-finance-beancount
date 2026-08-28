"""Общее для тестов раскладки выписок и команды `add`.

Список счетов здесь свой, а не боевой: боевой лежит рядом с леджером, в
приватном репозитории, и в тестах его быть не должно. Подобран так, чтобы
в нём встретился каждый случай, который раскладке приходится различать.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from finance import config
from finance.categorize import Rules
from finance.config import build_importers, load_accounts
from finance.inbox import Account, Inbox
from tests.fixtures import RULES

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden.py"

#: Два счёта в AMD различимы только по имени файла, два счёта в валюте — по
#: хвосту номера в описании процентов. Плюс счёт ACBA: у него метки нет вовсе,
#: номер лежит внутри файла, и переименование ему ничем не помогает.
ACCOUNTS = [
    {"bank": "ameria", "account": "Assets:Ameria:Card0001", "currency": "AMD",
     "marker": "card0001"},
    {"bank": "ameria", "account": "Assets:Ameria:Card0002", "currency": "AMD",
     "marker": "card0002"},
    {"bank": "ameria-account", "account": "Assets:Ameria:Usd", "currency": "USD",
     "marker": "usd", "number": "1000053294282901"},
    {"bank": "ameria-account", "account": "Assets:Ameria:Eur", "currency": "EUR",
     "marker": "eur", "number": "1000053294282902"},
    {"bank": "acba-card", "account": "Assets:Acba:Amd", "currency": "AMD",
     "number": "100000000000001"},
]


@pytest.fixture(scope="session")
def rules() -> Rules:
    return Rules.load(RULES)


@pytest.fixture
def inbox(tmp_path, rules) -> Inbox:
    """Раскладка со своим списком счетов и пустой папкой под inbox."""
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump({"accounts": ACCOUNTS}, allow_unicode=True), encoding="utf-8")
    specs = load_accounts(path)
    importers = build_importers(rules, specs)
    accounts = [
        Account(importer, spec["account"])
        for importer, spec in zip(importers, specs, strict=True)
    ]
    return Inbox(accounts, tmp_path / "inbox")


@pytest.fixture
def finance_env(tmp_path, monkeypatch) -> Path:
    """Подменить все три пути, по которым собирается раскладка.

    Нужно всем, кто зовёт `Inbox.build()` — она читает config в момент вызова.
    RULES ведёт к боевым правилам рядом с леджером, а его в CI нет вовсе, так
    что подмена здесь не ради изоляции, а чтобы тест вообще мог работать.
    Возвращается папка inbox: с ней тесты и имеют дело.

    Проверяется это так — леджер никуда не девается, просто конфиг смотрит
    мимо него: FINANCE_LEDGER=нет-такого pytest.
    """
    directory = tmp_path / "inbox"
    directory.mkdir()
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(
        yaml.safe_dump({"accounts": ACCOUNTS}, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setattr(config, "INBOX", directory)
    monkeypatch.setattr(config, "ACCOUNTS", accounts)
    monkeypatch.setattr(config, "RULES", RULES)
    monkeypatch.setattr(config, "OUT", tmp_path / "out.beancount")
    # Иначе бот завёл бы себе секрет вебхука на настоящем диске: RUN на
    # сервере указывает на том, а в тесте ему место только во временной папке.
    monkeypatch.setattr(config, "RUN", tmp_path / "run")
    return directory


def check_golden(name: str) -> None:
    """Сверить эталон и упасть с его выводом, если разошёлся.

    Подпроцессом, потому что сверка живёт в click-команде beangulp, а её
    pytest сам не подхватит. Заодно так проверяется и сам скрипт: тесты зовут
    его ровно так же, как позовёте вы руками.

    Разошлось после правки tests/rules.yaml — посмотрите дифф и, если он
    ожидаемый, перегенерируйте: `python tests/golden.py <имя> generate`.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(GOLDEN), name, "test"],
        cwd=ROOT,
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        # check=False: падать должен assert с выводом скрипта, а не сам вызов.
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def copy(source: Path, into: Path, name: str) -> Path:
    """Положить фикстуру под нужным именем: имя здесь и есть предмет проверки."""
    target = into / name
    target.write_bytes(source.read_bytes())
    return target


def account_named(inbox: Inbox, name: str) -> Account:
    return next(account for account in inbox.accounts if account.name == name)
