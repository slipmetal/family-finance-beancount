"""Список импортёров — по одному на счёт.

Сами счета описаны не здесь, а в `accounts.yaml` рядом с леджером: номера
счетов это личные данные, и в кодовом репозитории им не место. Образец формата
с вымышленными номерами — `accounts.example.yaml`.

Один и тот же список используют и CLI (`import.py`), и веб-интерфейс fava
(`fava_import_config.py`), чтобы они не разъезжались.

Добавить банк = дописать модуль в finance/importers/ и строку в BANKS.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import yaml

from finance.categorize import ACCOUNT_RE, Rules
from finance.importers import acba, ameria, sber, tbank

ROOT = Path(__file__).resolve().parents[1]

#: Леджер, правила и список счетов лежат вместе. При развёртывании — на томе,
#: локально — в клоне приватного репозитория `ledger/`.
LEDGER = Path(os.environ.get("FINANCE_LEDGER") or ROOT / "ledger")
INBOX = Path(os.environ.get("FINANCE_INBOX") or ROOT / "inbox")
RULES = Path(os.environ.get("FINANCE_RULES") or LEDGER / "rules.yaml")
ACCOUNTS = Path(os.environ.get("FINANCE_ACCOUNTS") or LEDGER / "accounts.yaml")


class ConfigError(Exception):
    """Ошибка в accounts.yaml. Сообщение всегда указывает, где именно."""


#: bank → (обязательные поля сверх account/currency, сборка импортёра).
BANKS: dict[str, tuple[tuple[str, ...], Callable[[dict, Rules], Any]]] = {
    "ameria": (
        ("marker",),
        lambda spec, rules: ameria.Importer(
            spec["account"], spec["currency"], rules, marker=spec["marker"]
        ),
    ),
    "acba-card": (
        ("number",),
        lambda spec, rules: acba.CardImporter(
            spec["account"], spec["currency"], spec["number"], rules
        ),
    ),
    "acba-account": (
        ("number",),
        lambda spec, rules: acba.AccountImporter(
            spec["account"], spec["currency"], spec["number"], rules
        ),
    ),
    "tbank": (
        ("number",),
        lambda spec, rules: tbank.Importer(
            spec["account"], spec["currency"], spec["number"], rules
        ),
    ),
    "sber": (
        ("number",),
        lambda spec, rules: sber.Importer(
            spec["account"], spec["currency"], spec["number"], rules
        ),
    ),
}

BASE_KEYS = ("bank", "account", "currency")


def load_accounts(path: Path | None = None) -> list[dict]:
    """Прочитать и проверить accounts.yaml.

    Всё проверяется здесь, на загрузке: неизвестный банк, забытое поле, опечатка
    в имени счёта. Иначе ошибка всплыла бы при импорте — и, что хуже, могла бы
    увести выписку не на тот счёт.
    """
    path = path if path is not None else ACCOUNTS
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            f"{path}: список счетов не найден. Он лежит рядом с леджером, "
            f"в приватном репозитории. Начать можно с образца: "
            f"cp accounts.example.yaml {path}"
        ) from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: не читается как YAML: {exc}") from None

    if not isinstance(raw, dict) or not isinstance(raw.get("accounts"), list):
        raise ConfigError(f"{path}: ожидался ключ `accounts` со списком счетов")
    if not raw["accounts"]:
        raise ConfigError(f"{path}: список счетов пуст, импортировать нечего")

    seen: dict[str, int] = {}
    specs = []
    for index, item in enumerate(raw["accounts"], start=1):
        spec = _parse_account(item, path=path, index=index)
        if spec["account"] in seen:
            raise ConfigError(
                f"{path}, счёт №{index}: {spec['account']} уже описан "
                f"под №{seen[spec['account']]}"
            )
        seen[spec["account"]] = index
        specs.append(spec)
    return specs


def _parse_account(item: Any, *, path: Path, index: int) -> dict:
    where = f"{path}, счёт №{index}"
    if not isinstance(item, dict):
        raise ConfigError(f"{where}: ожидался набор полей, а не {type(item).__name__}")

    missing = [key for key in BASE_KEYS if not item.get(key)]
    if missing:
        raise ConfigError(f"{where}: не заполнены обязательные поля {missing}")

    bank = item["bank"]
    if bank not in BANKS:
        raise ConfigError(
            f"{where}: неизвестный банк {bank!r}, известны {sorted(BANKS)}"
        )
    extra_keys, _ = BANKS[bank]

    allowed = {*BASE_KEYS, *extra_keys}
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise ConfigError(f"{where}: неизвестные поля {unknown}, для {bank!r} ожидались {sorted(allowed)}")

    missing = [key for key in extra_keys if not item.get(key)]
    if missing:
        raise ConfigError(f"{where}: для банка {bank!r} обязательны поля {missing}")

    if not ACCOUNT_RE.match(str(item["account"])):
        raise ConfigError(
            f"{where}: {item['account']!r} не похоже на имя счёта beancount "
            f"(например, Assets:Acba:Amd)"
        )

    # Номера счетов состоят из цифр, и YAML охотно превращает их в int. Строка
    # нужна, чтобы сравнение с номером из выписки не зависело от ведущих нулей.
    return {**item, **{key: str(item[key]) for key in extra_keys}}


def build_importers(rules: Rules | None = None, accounts: list[dict] | None = None) -> list:
    """Собрать импортёры для всех счетов из accounts.yaml.

    Правила загружаются один раз и переиспользуются: они общие для всех банков.
    """
    rules = rules if rules is not None else Rules.load(RULES)
    specs = accounts if accounts is not None else load_accounts()
    return [BANKS[spec["bank"]][1](spec, rules) for spec in specs]