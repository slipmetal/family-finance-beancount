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
from collections import Counter
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
#: Куда `import.py archive` убирает разобранные выписки. Личных данных полно,
#: поэтому в кодовом репозитории эта папка в .gitignore, а на сервере — на томе.
DOCUMENTS = Path(os.environ.get("FINANCE_DOCUMENTS") or ROOT / "documents")
RULES = Path(os.environ.get("FINANCE_RULES") or LEDGER / "rules.yaml")
ACCOUNTS = Path(os.environ.get("FINANCE_ACCOUNTS") or LEDGER / "accounts.yaml")


class ConfigError(Exception):
    """Ошибка в accounts.yaml. Сообщение всегда указывает, где именно."""


#: bank → (обязательные поля сверх account/currency, сборка импортёра).
BANKS: dict[str, tuple[tuple[str, ...], Callable[[dict, Rules], Any]]] = {
    "ameria": (
        ("marker",),
        lambda spec, rules: ameria.CardImporter(
            spec["account"], spec["currency"], rules,
            marker=spec["marker"], marker_optional=spec.get(MARKER_OPTIONAL, False),
        ),
    ),
    # Метка нужна, потому что номера счёта в файле нет; номер — потому что
    # в описании процентов банк печатает его хвост, и это единственная
    # возможность перепроверить метку по содержимому.
    "ameria-account": (
        ("marker", "number"),
        lambda spec, rules: ameria.AccountImporter(
            spec["account"], spec["currency"], rules,
            marker=spec["marker"], number=spec["number"],
            marker_optional=spec.get(MARKER_OPTIONAL, False),
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

#: Служебное поле, которое build_importers дописывает в spec перед сборкой.
#: В accounts.yaml его не пишут — оно вычисляется по всему списку счетов.
MARKER_OPTIONAL = "marker_optional"

#: Сколько цифр номера счёта минимум печатает Ameriabank в описании процентов —
#: см. ACCOUNT_TAIL_RE в finance/importers/ameria.py. Импортёр сверяет номер
#: с хвостом такой длины, значит два счёта, у которых совпадают последние шесть
#: цифр, по содержимому выписки неразличимы.
AMERIA_TAIL = 6


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


def optional_markers(specs: list[dict]) -> set[str]:
    """Счета Ameriabank, выписку которых можно не переименовывать.

    Метка в имени файла нужна там, где счёт неотличим по содержимому выписки:
    номера счёта Ameriabank в файл не кладёт. Но кое-что кладёт, и если этого
    хватает, чтобы отличить счёт от ОСТАЛЬНЫХ счетов того же формата, метка
    становится необязательной.

    Что служит признаком, зависит от формата (см. finance/importers/ameria.py):

    * карточная выписка — код валюты в сумме. Значит две карты в AMD метку
      требуют, а единственная карта в RUB — нет;
    * выписка по счёту — хвост номера в описании процентов. Валюты в этом
      формате нет вообще, поэтому счета сравниваются по номерам.

    Считать это может только тот, кому видны все счета разом: сам импортёр
    не знает, есть ли у него двойник. Отсюда и функция здесь, а не там.

    Уникальность именно доказывается по accounts.yaml, а не угадывается по
    файлу: если двойник есть, метка остаётся обязательной, и безымянная выписка
    просто не опознаётся — это лучше, чем уехать не на тот счёт.
    """
    # Номера сравниваем по хвосту той длины, которой оперирует импортёр:
    # у более длинного совпадения хвост короче не станет.
    def tail(spec: dict) -> str:
        return str(spec.get("number", ""))[-AMERIA_TAIL:]

    currencies = Counter(spec["currency"] for spec in specs if spec["bank"] == "ameria")
    tails = Counter(tail(spec) for spec in specs if spec["bank"] == "ameria-account")

    unique = {
        "ameria": lambda spec: currencies[spec["currency"]] == 1,
        "ameria-account": lambda spec: bool(tail(spec)) and tails[tail(spec)] == 1,
    }
    return {
        spec["account"]
        for spec in specs
        if spec["bank"] in unique and unique[spec["bank"]](spec)
    }


def build_importers(rules: Rules | None = None, accounts: list[dict] | None = None) -> list:
    """Собрать импортёры для всех счетов из accounts.yaml.

    Правила загружаются один раз и переиспользуются: они общие для всех банков.
    """
    rules = rules if rules is not None else Rules.load(RULES)
    specs = accounts if accounts is not None else load_accounts()
    optional = optional_markers(specs)
    return [
        BANKS[spec["bank"]][1](
            {**spec, MARKER_OPTIONAL: spec["account"] in optional}, rules
        )
        for spec in specs
    ]